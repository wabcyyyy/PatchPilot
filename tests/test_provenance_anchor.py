"""P3-2 溯源硬化测试:脏树指纹与输入锚点反查。

背景(审计 R1-Q2-1):fake36 批 manifest 记 git_commit=367ae36,但该 commit 上
只有 BUG-001..028——批次实际跑在"E5 已迁移文件、ca17ccc 未提交"的脏工作树上,
锚点上不存在所跑的 7 道新题,无法复现;而夜报把这个坏锚当成功证据引用。

修法:①provenance/manifest 记录工作树状态(worktree_dirty/dirty_fingerprint,
None=无法判定,空指纹=干净);②manifest 落盘前 ls-tree 反查每题在锚点 commit
是否存在,缺失即留痕+告警。测试用 monkeypatch _run_git 注入 git 输出,
不依赖真实工作树状态(开发树常是脏的,断言"干净"会假失败)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evals import provenance
from app.evals.provenance import (
    dirty_fingerprint,
    missing_inputs_at_commit,
    worktree_dirty,
)

BUG_ROOT = Path("bugs")


def _stub_git(monkeypatch: pytest.MonkeyPatch, outputs: dict[tuple[str, ...], str | None]) -> None:
    """按命令前缀注入 git 输出;未列出的命令返回 None(模拟失败)。"""

    def fake_run(args: list[str], timeout: float = 10) -> str | None:
        for prefix, value in outputs.items():
            if tuple(args[: len(prefix)]) == prefix:
                return value
        return None

    monkeypatch.setattr(provenance, "_run_git", fake_run)


# ---------- 工作树状态 ----------


def test_clean_tree_is_false_with_empty_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(monkeypatch, {("status", "--porcelain"): ""})
    assert worktree_dirty() is False
    assert dirty_fingerprint() == ""


def test_dirty_tree_flag_and_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(
        monkeypatch,
        {
            ("status", "--porcelain"): " M app/config.py\n?? demo/x.py\n",
            ("diff", "HEAD"): "diff --git a/app/config.py ...\n-old\n+new\n",
        },
    )
    assert worktree_dirty() is True
    fp = dirty_fingerprint()
    assert len(fp) == 16 and all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_distinguishes_content_not_just_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指纹含 tracked 改动全文:同名文件的纯内容改动也产生不同指纹。"""
    status = " M app/config.py\n"
    _stub_git(monkeypatch, {("status", "--porcelain"): status, ("diff", "HEAD"): "-a\n+b\n"})
    first = dirty_fingerprint()
    _stub_git(monkeypatch, {("status", "--porcelain"): status, ("diff", "HEAD"): "-a\n+c\n"})
    second = dirty_fingerprint()
    assert first != second
    _stub_git(monkeypatch, {("status", "--porcelain"): status, ("diff", "HEAD"): "-a\n+b\n"})
    assert dirty_fingerprint() == first  # 同输入同指纹(确定性)


def test_git_unavailable_is_none_not_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """无法判定 ≠ 干净:git 失败时 worktree_dirty 返回 None(写入 JSON 为 null),
    指纹为空串——读者不得把 null 读成"干净"。"""
    _stub_git(monkeypatch, {})  # 全部命令返回 None
    assert worktree_dirty() is None
    assert dirty_fingerprint() == ""


# ---------- 输入锚点反查 ----------

_LS_TREE = "bugs/BUG-001/manifest.yaml\nbugs/BUG-002/repo/src/a.py\nbugs/BUG-002/manifest.yaml\nbugs/README.md\n"


def test_missing_inputs_detects_absent_bug_at_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """锚点 commit 上只有 BUG-001/002,而批次跑了 003 → 缺失清单含 003。"""
    _stub_git(monkeypatch, {("ls-tree", "-r", "--name-only"): _LS_TREE})
    missing = missing_inputs_at_commit("deadbeef" * 5, ["BUG-001", "BUG-002", "BUG-003"])
    assert missing == ["BUG-003"]


def test_missing_inputs_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(monkeypatch, {("ls-tree", "-r", "--name-only"): _LS_TREE})
    assert missing_inputs_at_commit("deadbeef" * 5, ["BUG-001", "BUG-002"]) == []


def test_missing_inputs_unchecked_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """unknown commit / git 失败 → None = 未检查,与"检查通过(空列表)"区分。"""
    _stub_git(monkeypatch, {("ls-tree", "-r", "--name-only"): _LS_TREE})
    assert missing_inputs_at_commit("unknown", ["BUG-001"]) is None
    assert missing_inputs_at_commit("", ["BUG-001"]) is None
    _stub_git(monkeypatch, {})  # ls-tree 失败
    assert missing_inputs_at_commit("deadbeef" * 5, ["BUG-001"]) is None


# ---------- 端到端:provenance 与 manifest 落盘 ----------


def test_build_provenance_carries_worktree_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_git(
        monkeypatch,
        {
            ("rev-parse", "HEAD"): "a" * 40,
            ("status", "--porcelain"): " M x.py\n",
            ("diff", "HEAD"): "d",
        },
    )
    prov = provenance.build_provenance("fake-replay", "", "plain")
    assert prov["worktree_dirty"] is True and len(prov["dirty_fingerprint"]) == 16


def test_manifest_records_anchor_check_and_dirty_state(tmp_path: Path) -> None:
    """端到端:fake 批 manifest 带工作树状态与锚点反查结果;本仓库 HEAD 上
    BUG-001 必然存在 → checked=True 且 missing 为空(真实 git,非 stub)。"""
    from app.evals.bugset import load_bug, load_replay_script
    from app.evals.driver import run_batch
    from app.llm.fake import FakeLLM

    bug = load_bug("BUG-001", BUG_ROOT)
    run_batch([bug], lambda b: FakeLLM(load_replay_script(b)), runs_root=tmp_path / "b")
    manifest = json.loads((tmp_path / "b" / "batch_manifest.json").read_text(encoding="utf-8"))

    assert set(manifest) == {
        "git_commit",
        "started_at",
        "ended_at",
        "config_snapshot",
        "model",
        "bug_ids",
        "verdict_counts",
        "blind",
        "worktree_dirty",
        "dirty_fingerprint",
        "input_anchor_checked",
        "input_anchor_missing",
    }
    assert manifest["input_anchor_checked"] is True
    assert manifest["input_anchor_missing"] == []
    assert manifest["worktree_dirty"] in (True, False, None)
    # 字段自洽:脏 ⇒ 有指纹;指纹 ⇒ 脏(空串 ↔ false/None 只允许干净或未判定)
    if manifest["worktree_dirty"] is True:
        assert len(manifest["dirty_fingerprint"]) == 16
    else:
        assert manifest["dirty_fingerprint"] == ""
