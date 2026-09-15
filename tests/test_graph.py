"""M5 LangGraph 状态机测试:覆盖企划书 4.2 转移表的主要分支。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.evals.bugset import load_bug
from app.graph.builder import build_graph
from app.graph.nodes import READ_TOOLS, TaskNodes
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

BUG_ROOT = Path("bugs")
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"


def _fix_diff() -> str:
    """BUG-001 的正确修复 diff(临时副本上生成)。"""
    tmp = Path(tempfile.mkdtemp(prefix="gfix-"))
    try:
        from app.gitops.differ import working_tree_diff
        from app.gitops.snapshot import create_workspace
        from app.gitops.testing import materialize_repo

        repo_src = tmp / "src"
        materialize_repo(BUG_ROOT / "BUG-001" / "repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "    if value is None:\n        return None\n",
                "    if value is None:\n        return None\n    if not value.strip():\n        return None\n",
            ),
            encoding="utf-8",
            newline="\n",
        )
        return working_tree_diff(tmp / "ws").diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _comment_diff() -> str:
    """不修复问题的合法补丁(只加注释),用于制造"verify 失败但门禁通过"的场景。"""
    tmp = Path(tempfile.mkdtemp(prefix="gcomment-"))
    try:
        from app.gitops.differ import working_tree_diff
        from app.gitops.snapshot import create_workspace
        from app.gitops.testing import materialize_repo

        repo_src = tmp / "src"
        materialize_repo(BUG_ROOT / "BUG-001" / "repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                '    raise ValueError(f"unrecognized date format: {value!r}")\n',
                '    raise ValueError(f"unrecognized date format: {value!r}")  # noqa: 见 issue\n',
            ),
            encoding="utf-8",
            newline="\n",
        )
        return working_tree_diff(tmp / "ws").diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _localize_script() -> list[dict]:
    return [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {
            "tool": "finish",
            "args": {"success": True, "summary": "根因:parse_date 未处理空白字符串"},
        },
    ]


def _propose_script(diff_text: str) -> list[dict]:
    return [
        {"tool": "apply_patch", "args": {"diff_text": diff_text}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "补丁已应用且测试通过"}},
    ]


def test_graph_resolves_bug001(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.engine == "graph"
    assert result.status == "FINISHED" and result.verdict == "resolved"
    assert result.rounds == 1
    assert result.changed_files == ["src/dateparse.py"]
    assert (Path(result.run_dir) / "trajectory.jsonl").exists()
    assert (Path(result.run_dir) / "checkpoints.sqlite").exists()


def test_graph_invalid_task(tmp_path: Path) -> None:
    bug = load_bug("BUG-003", BUG_ROOT)
    bug_dir = tmp_path / "BUG-003-FIXED"
    shutil.copytree(bug.root, bug_dir)
    (bug_dir / "repo" / "src" / "labels.py").write_text(
        'def join_labels(labels, sep=None):\n    separator = "," if sep is None else sep\n'
        '    result = ""\n    for index, label in enumerate(labels):\n'
        "        if index > 0:\n            result += separator\n        result += label\n    return result\n",
        encoding="utf-8",
        newline="\n",
    )
    fixed_bug = load_bug(bug_dir)
    model = FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "nothing"}}])
    result = run_task_graph(fixed_bug, model, runs_root=tmp_path / "runs")
    assert result.status == "INVALID_TASK"


def test_graph_patch_rejected_then_retry_resolves(tmp_path: Path) -> None:
    """第 1 轮补丁(改测试文件)被拒 → 回 PROPOSE(round=2)→ 正确补丁 → resolved。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    bad_patch = [
        {
            "tool": "apply_patch",
            "args": {
                "diff_text": (
                    "diff --git a/tests/test_dateparse.py b/tests/test_dateparse.py\n"
                    "--- a/tests/test_dateparse.py\n"
                    "+++ b/tests/test_dateparse.py\n"
                    "@@ -1,3 +1,4 @@\n"
                    " import pytest\n"
                    "+\n"
                    " from src.dateparse import parse_date\n"
                )
            },
        },
        {"tool": "finish", "args": {"success": True, "summary": "试图改测试"}},
    ]
    model = FakeLLM(_localize_script() + bad_patch + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved"
    assert result.rounds == 2  # 第 1 轮被拒,第 2 轮修复
    assert result.changed_files == ["src/dateparse.py"]  # 回滚生效,坏补丁没有残留


def test_graph_budget_exhausted_after_failed_verify(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_comment_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=1)

    assert result.status == "BUDGET_EXCEEDED"
    assert result.verdict == "failed"
    assert result.verify_failed_ok is False


def test_graph_verify_failed_when_agent_gives_up(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    give_up = [{"tool": "finish", "args": {"success": False, "summary": "我修不了"}}]
    model = FakeLLM(_localize_script() + give_up)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "VERIFY_FAILED" and result.verdict == "failed"


def test_phase_tool_restriction(tmp_path: Path) -> None:
    """定位阶段禁用 apply_patch:工具调用被拒并记录轨迹。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    from app.gitops.testing import materialize_repo

    work = tmp_path / "ws"
    baseline = materialize_repo(bug.repo_dir, work, extra_commit=False)
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="T-PHASE")
    ctx = ToolContext(
        task_id="T-PHASE",
        workspace=work,
        baseline_commit=baseline,
        tracker=tracker,
        report_dir=tmp_path / "reports",
        test_sets=bug.test_sets,
    )
    model = FakeLLM(
        _localize_script()[:1]
        + [
            {"tool": "apply_patch", "args": {"diff_text": "junk"}},
            {"tool": "finish", "args": {"success": True, "summary": "x"}},
        ]
    )
    from app.graph.plain_loop import run_plain_loop

    run_plain_loop(ctx, model, "issue", allowed_tools=READ_TOOLS, state_label="LOCALIZE")
    events = [e for e in tracker.events if e.tool == "apply_patch"]
    assert events and "not allowed in phase" in (events[0].error or "")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    """带 SqliteSaver 执行完整任务后,同 thread_id 可从 checkpoint 恢复出最终状态。"""
    from app.graph.checkpoint import make_sqlite_checkpointer
    from app.graph.state import TaskState

    bug = load_bug("BUG-001", BUG_ROOT)
    cp_db = tmp_path / "cp" / "checkpoints.sqlite"
    checkpointer = make_sqlite_checkpointer(cp_db)
    assert checkpointer is not None

    nodes = TaskNodes(
        bug=bug,
        model=FakeLLM(_localize_script() + _propose_script(_fix_diff())),
        workspace=tmp_path / "ws",
        tracker=Tracker(None, task_id="T-CP"),
        report_dir=tmp_path / "reports",
        max_rounds=bug.max_rounds,
        max_turns=20,
        started_monotonic=__import__("time").monotonic(),
    )
    graph = build_graph(nodes, checkpointer=checkpointer)
    thread = {"configurable": {"thread_id": "T-CP"}}
    final: TaskState = graph.invoke(
        {
            "bug_id": bug.id,
            "issue_text": bug.issue_text,
            "failed_tests": bug.failed_tests,
            "regression_tests": bug.regression_tests,
            "allowed_paths": bug.allowed_paths,
            "max_rounds": bug.max_rounds,
            "status": "CREATED",
            "round_no": 1,
            "turns": 0,
            "tokens_used": 0,
        },
        config=thread,
    )
    assert final["status"] == "FINISHED"

    resumed: TaskState = graph.invoke(None, config=thread)
    assert resumed["status"] == "FINISHED" and resumed["outcome"] == "resolved"
    assert cp_db.exists() and cp_db.stat().st_size > 0


def test_graph_needs_review_when_localize_fails(tmp_path: Path) -> None:
    """定位阶段模型声明失败 → NEEDS_REVIEW 转人工,不产生补丁。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    give_up = [{"tool": "finish", "args": {"success": False, "summary": "找不到根因"}}]
    model = FakeLLM(give_up + [{"tool": "finish", "args": {"success": True, "summary": "unused"}}])
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "NEEDS_REVIEW" and result.verdict == "needs_review"
    assert result.verify_failed_ok is False
