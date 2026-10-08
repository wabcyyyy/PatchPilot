"""S05a/F4:规范 TaskSpec 的身份矩阵、源指纹、重建与落盘。

核心验收(review F4):failed=[a],regression=[b,c] 与 failed=[a,b],regression=[c]
必须得到**不同**哈希——旧 _custom_bug_id 的裸拼接把两种任务身份折叠成同一个。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.evals.bugset import load_bug
from app.task_spec import (
    SCHEMA_VERSION,
    TaskSpec,
    build_task_spec,
    canonical_json,
    fingerprint_source_dir,
    read_source_commit,
    stable_hash,
    write_task_spec_file,
)

BUG_ROOT = Path("bugs")


def _make_spec(tmp_path: Path, *, failed=None, regression=None, issue="issue", **overrides):
    src = tmp_path / "repo"
    (src / "src").mkdir(parents=True)
    (src / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.issue_text = issue
    if failed is not None:
        bug.failed_tests = failed
    if regression is not None:
        bug.regression_tests = regression
    bug.repo_dir = src
    return build_task_spec(
        bug, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20, **overrides
    )


def test_f4_split_tests_now_yield_different_hashes(tmp_path: Path) -> None:
    a = _make_spec(tmp_path / "a", failed=["t::a"], regression=["t::b", "t::c"])
    b = _make_spec(tmp_path / "b", failed=["t::a", "t::b"], regression=["t::c"])
    assert a.task_spec_hash != b.task_spec_hash, "字段边界不得通过裸拼接折叠"


def test_same_inputs_give_stable_hash_and_time_excluded(tmp_path: Path) -> None:
    # 同一源目录 + 同输入:重复构建身份逐字节稳定(时间不在内容身份里)
    bug_a, bug_b = _bug_for(tmp_path / "same"), _bug_for(tmp_path / "same")
    a = build_task_spec(
        bug_a, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20
    )
    b = build_task_spec(
        bug_b, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20
    )
    assert a.task_spec_hash == b.task_spec_hash
    # 时间记录在落盘形态里,但不参与内容身份(两次构建内容一致即同哈希)
    assert "created_at" in a.to_json_dict()
    assert "created_at" not in a.content_dict()


def test_test_order_is_part_of_identity(tmp_path: Path) -> None:
    a = _make_spec(tmp_path / "a", failed=["t::a", "t::b"])
    b = _make_spec(tmp_path / "b", failed=["t::b", "t::a"])
    assert a.task_spec_hash != b.task_spec_hash, "测试列表保留实际执行顺序,顺序即身份"


def test_issue_length_and_content_are_identity(tmp_path: Path) -> None:
    long_issue = "很长的缺陷描述" * 200  # 远超旧持久化的 500 字符
    a = _make_spec(tmp_path / "a", issue=long_issue)
    assert len(a.issue_text) == len(long_issue)
    assert a.to_json_dict()["issue_text"] == long_issue, "issue 完整保存,不截到 500"
    b = _make_spec(tmp_path / "b", issue=long_issue + "!")
    assert a.task_spec_hash != b.task_spec_hash


def test_allowed_paths_empty_list_normalizes_to_none(tmp_path: Path) -> None:
    bug = _bug_for(tmp_path / "a")
    bug.allowed_paths = []
    empty = build_task_spec(
        bug, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20
    )
    assert empty.allowed_paths is None, "空列表按既有 None 语义规范化,不趁机改 scope"
    bug2 = _bug_for(tmp_path / "b")
    bug2.allowed_paths = ["src/"]
    scoped = build_task_spec(
        bug2, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20
    )
    assert scoped.allowed_paths == ["src/"]


def test_policy_and_model_and_replay_change_identity(tmp_path: Path) -> None:
    base = _make_spec(tmp_path / "a")

    from app.config import get_settings

    alt_settings = get_settings().model_copy(update={"token_budget": 12345})
    alt = build_task_spec(
        _bug_for(tmp_path / "a"),
        engine="graph",
        arm="agent",
        model_provider="fake-replay",
        max_turns=20,
        settings=alt_settings,
    )
    assert base.task_spec_hash != alt.task_spec_hash, "有效策略冻结进身份"

    other_model = _make_spec(tmp_path / "b", model_name="gpt-x")
    assert base.task_spec_hash != other_model.task_spec_hash

    with_replay = build_task_spec(
        _bug_for(tmp_path / "c"),
        engine="graph",
        arm="agent",
        model_provider="fake-replay",
        max_turns=20,
        replay=[{"tool": "finish", "args": {"success": True}}],
    )
    assert base.task_spec_hash != with_replay.task_spec_hash, "回放脚本是任务身份的一部分"


def _bug_for(tmp_path: Path):
    src = tmp_path / "repo"
    (src / "src").mkdir(parents=True, exist_ok=True)
    (src / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.repo_dir = src
    return bug


def test_roundtrip_preserves_identity(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    payload = spec.to_json_dict()
    restored = TaskSpec.from_json_dict(json.loads(canonical_json(payload)))
    assert restored.task_spec_hash == spec.task_spec_hash
    assert restored.issue_text == spec.issue_text


def test_from_json_rejects_tamper_and_unknown_schema(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    payload = spec.to_json_dict()
    payload["issue_text"] += "tampered"
    with pytest.raises(ValueError, match="mismatch"):
        TaskSpec.from_json_dict(payload)
    payload = spec.to_json_dict()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        TaskSpec.from_json_dict(payload)


# ---------- 源指纹 ----------


def _mk_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return root


def test_fingerprint_same_bytes_same_hash_and_mtime_ignored(tmp_path: Path) -> None:
    a, b = _mk_repo(tmp_path / "a"), _mk_repo(tmp_path / "b")
    fa, fb = fingerprint_source_dir(a), fingerprint_source_dir(b)
    assert fa == fb
    os.utime(a / "src" / "mod.py", (0, 0))  # mtime 变化不改变身份
    assert fingerprint_source_dir(a) == fa


def test_fingerprint_changes_on_content_add_delete(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path / "r")
    base = fingerprint_source_dir(repo)
    (repo / "src" / "extra.py").write_text("y = 2\n", encoding="utf-8")
    with_extra = fingerprint_source_dir(repo)
    assert with_extra != base
    (repo / "src" / "extra.py").unlink()
    assert fingerprint_source_dir(repo) == base
    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    assert fingerprint_source_dir(repo) != base


def test_fingerprint_excludes_git_dir(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path / "r")
    base = fingerprint_source_dir(repo)
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert fingerprint_source_dir(repo) == base


def test_read_source_commit_none_for_fixture(tmp_path: Path) -> None:
    assert read_source_commit(_mk_repo(tmp_path / "r")) is None


# ---------- 落盘与 schema 版本 ----------


def test_write_task_spec_file_is_canonical_and_roundtrips(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    returned = write_task_spec_file(tmp_path, spec)
    assert returned == spec.task_spec_hash
    raw = (tmp_path / "task_spec.json").read_text(encoding="utf-8")
    assert raw == canonical_json(json.loads(raw)), "落盘即规范形态"
    restored = TaskSpec.read_file(tmp_path / "task_spec.json")
    assert restored.task_spec_hash == spec.task_spec_hash
    payload = json.loads(raw)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["created_at"]  # 时间在落盘形态里,但不参与身份


def test_stable_hash_is_insensitive_to_dict_order() -> None:
    assert stable_hash({"a": 1, "b": [2, 3]}) == stable_hash({"b": [2, 3], "a": 1})


# ---------- runner 集成:契约落盘与 source_changed ----------


def test_run_task_graph_writes_task_spec_contract(tmp_path: Path) -> None:
    from app.evals.driver import run_task as _  # noqa: F401  (确认导入面存在)
    from app.graph.runner import run_task_graph
    from app.llm.fake import FakeLLM

    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(bug, FakeLLM([]), runs_root=tmp_path, task_id="T-SPEC")
    assert result.status in {"NEEDS_REVIEW"}  # 空脚本:finish 失败,任务合法收敛
    spec_file = Path(result.run_dir) / "task_spec.json"
    assert spec_file.exists(), "graph 任务必须落 task_spec.json"
    spec = TaskSpec.read_file(spec_file)
    assert spec.task_spec_hash
    assert spec.issue_text == bug.issue_text
    assert spec.failed_tests == bug.failed_tests
    assert spec.engine == "graph" and spec.max_turns == 20
    assert spec.source_kind == "manifest"
    # fake-replay 的完整回放脚本进契约(任务身份的一部分);真实模型才是 None
    assert isinstance(spec.replay, list) and spec.replay
    assert spec.effective_policy.get("token_budget") is not None


def test_run_task_graph_rejects_source_changed(tmp_path: Path) -> None:
    """受理后源内容被改:执行前重算指纹不一致 → INVALID_TASK/source_changed,不调模型。"""
    import shutil

    from app.graph.runner import run_task_graph
    from app.llm.fake import FakeLLM

    src = tmp_path / "repo-src"
    shutil.copytree(BUG_ROOT / "BUG-001" / "repo", src)
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.repo_dir = src

    calls = {"n": 0}

    class _CountingFake(FakeLLM):
        def complete(self, messages, tools):
            calls["n"] += 1
            return super().complete(messages, tools)

    result = run_task_graph(bug, _CountingFake([]), runs_root=tmp_path, task_id="T-CHG")
    assert result.status != "INVALID_TASK", "首次运行源内容一致,不因指纹拒绝"
    spec_file = tmp_path / "T-CHG" / "task_spec.json"
    assert spec_file.exists()
    accepted_hash = TaskSpec.read_file(spec_file).task_spec_hash
    # 同一 task_id 再次执行时源内容被改:对照受理契约重算 → INVALID_TASK/source_changed
    (src / "src" / "dateparse.py").write_text("def parse_date(v): return v\n", encoding="utf-8")
    before = calls["n"]
    result2 = run_task_graph(bug, _CountingFake([]), runs_root=tmp_path, task_id="T-CHG")
    assert result2.status == "INVALID_TASK"
    assert "source_changed" in (result2.error or "")
    assert calls["n"] == before, "源内容不符时不得调用模型"
    # 受理契约未被改写(拒绝执行前保全现场)
    assert TaskSpec.read_file(spec_file).task_spec_hash == accepted_hash
