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


def test_untracked_only_is_not_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    """只统计 tracked 改动:仅有未跟踪文件(本仓库长期存在 .idea//demo/)时
    worktree_dirty=False、指纹空——否则本标志在本仓库永远为真、失去操作性。
    未跟踪内容若恰是批次输入,由 manifest 的 input_anchor_missing 暴露。"""
    _stub_git(monkeypatch, {("status", "--porcelain"): "?? .idea/\n?? demo/\n"})
    assert worktree_dirty() is False
    assert dirty_fingerprint() == ""


def test_staged_change_also_counts_as_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    """staged(A/M/D 前缀)与未暂存改动同等计入——都属于"锚点之外的状态"。"""
    for porcelain in ("A  bugs/BUG-099/manifest.yaml\n", "M  app/config.py\n", "D  old.py\n"):
        _stub_git(monkeypatch, {("status", "--porcelain"): porcelain, ("diff", "HEAD"): "x"})
        assert worktree_dirty() is True, porcelain
        assert len(dirty_fingerprint()) == 16, porcelain


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


def test_build_provenance_carries_reproduction_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复盘 P0-3:max_turns/temperature/seed 三键必须在场——缺席即复现口径断裂;
    客户端不设置采样参数时显式 None(不可重放),不猜测数值。"""
    _stub_git(monkeypatch, {("rev-parse", "HEAD"): "a" * 40, ("status", "--porcelain"): ""})
    prov = provenance.build_provenance("fake-replay", "m", "plain", max_turns=7)
    assert prov["max_turns"] == 7
    assert prov["temperature"] is None
    assert prov["seed"] is None
    # 不传 max_turns(历史调用方)时也必须有键,值为 None
    prov_default = provenance.build_provenance("fake-replay", "m", "graph")
    assert prov_default["max_turns"] is None


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


# ---------- 取证命令解码(Windows 区域编码回归) ----------


def test_run_git_decodes_utf8_not_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取证 git 输出含中文(脏树 diff)时不得因区域编码(GBK)截断/崩读。

    修复前实测:Windows 下 `diff HEAD` 的中文内容使读取线程抛 UnicodeDecodeError,
    stdout 静默变空,脏树指纹退化成"只哈希文件名名单"——识别脏树的效力失真。
    """
    from app.gitops.cmd import run_git

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "t@patchpilot.local")
    run_git(repo, "config", "user.name", "T")
    (repo / "note.txt").write_text("原始内容\n", encoding="utf-8", newline="\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "中文提交信息")
    (repo / "note.txt").write_text("中文改动\n", encoding="utf-8", newline="\n")

    monkeypatch.setattr(provenance, "_REPO_ROOT", repo)
    out = provenance._run_git(["diff", "HEAD"])
    assert out is not None
    assert "中文改动" in out
