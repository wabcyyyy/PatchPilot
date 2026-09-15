"""M3 工具层测试:七个工具的边界校验与轨迹记录。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gitops.differ import working_tree_diff
from app.gitops.snapshot import create_workspace
from app.tools.base import ToolContext
from app.tools.execution import run_tests as run_tests_tool
from app.tools.files import list_files, read_file, search_code
from app.tools.patching import apply_patch, git_diff, reset_to_baseline
from app.tools.registry import execute
from app.tools.tracker import Tracker

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]


@pytest.fixture()
def task_ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="T-TEST")
    return ToolContext(
        task_id="T-TEST",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=tracker,
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": REGRESSION_IDS},
        allowed_paths=None,
    )


def _guard_diff() -> str:
    """生成 demo_repo 的"正确修复"diff:空/空白输入返回 None。"""
    import shutil
    import tempfile

    from app.gitops.testing import materialize_repo

    tmp = Path(tempfile.mkdtemp(prefix="guarddiff-"))
    try:
        repo_src = tmp / "src"
        materialize_repo(Path(__file__).parent / "fixtures" / "demo_repo", repo_src)
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


# ---------- 只读工具 ----------


def test_list_files_skips_git_and_lists_sources(task_ctx: ToolContext) -> None:
    result = list_files(task_ctx)
    assert result.ok
    files = result.output["files"]
    assert "src/dateparse.py" in files and "tests/test_dateparse.py" in files
    # 排除的是 .git 目录;.gitignore 是合法的被列出文件
    assert not any(f == ".git" or f.startswith(".git/") for f in files)


def test_read_file_offset_and_missing(task_ctx: ToolContext) -> None:
    result = read_file(task_ctx, "src/dateparse.py")
    assert result.ok and result.output["total_lines"] > 0
    assert "parse_date" in result.output["content"]

    assert not read_file(task_ctx, "src/missing.py").ok
    assert not read_file(task_ctx, "../outside.py").ok
    assert not read_file(
        task_ctx, str(task_ctx.workspace / "src" / "dateparse.py")
    ).ok  # 绝对路径拒绝


def test_read_file_line_limit(task_ctx: ToolContext) -> None:
    task_ctx.max_read_lines = 5
    result = read_file(task_ctx, "src/dateparse.py", offset=3)
    assert result.ok
    assert result.output["returned_lines"] <= 5
    assert result.output["truncated"]


def test_search_code_matches_and_requires_keyword(task_ctx: ToolContext) -> None:
    result = search_code(task_ctx, "parse_date")
    assert result.ok and result.output["total"] >= 2
    assert all("parse_date" in m["text"].lower() for m in result.output["matches"])
    assert not search_code(task_ctx, "  ").ok


# ---------- 变更工具 ----------


def test_apply_patch_rejects_test_file_modification(task_ctx: ToolContext) -> None:
    diff = (
        "diff --git a/tests/test_dateparse.py b/tests/test_dateparse.py\n"
        "--- a/tests/test_dateparse.py\n"
        "+++ b/tests/test_dateparse.py\n"
        "@@ -1,3 +1,4 @@\n"
        " from src.dateparse import parse_date\n"
        "+\n"
        " def test_x():\n"
        "     assert True\n"
    )
    result = apply_patch(task_ctx, diff)
    assert not result.ok
    assert "test file" in result.error


def test_apply_patch_rejects_outside_allowed_paths(task_ctx: ToolContext) -> None:
    task_ctx.allowed_paths = ["othersrc/**"]
    result = apply_patch(task_ctx, _guard_diff())
    assert not result.ok
    assert "allowed scope" in result.error


def test_apply_git_diff_reset_roundtrip(task_ctx: ToolContext) -> None:
    diff_text = _guard_diff()
    result = apply_patch(task_ctx, diff_text)
    assert result.ok, result.error
    assert "src/dateparse.py" in result.output["changed_files"]

    shown = git_diff(task_ctx)
    assert shown.ok and "src/dateparse.py" in shown.output["changed_files"]

    reset = reset_to_baseline(task_ctx)
    assert reset.ok
    assert git_diff(task_ctx).output["is_empty"]


def test_run_tests_tool_reports_failure_and_pass(task_ctx: ToolContext) -> None:
    failed_run = run_tests_tool(task_ctx, "failed")
    assert failed_run.ok
    assert not failed_run.output["all_passed"]
    assert failed_run.output["failed"] == 1
    assert failed_run.output["failed_cases"][0]["name"] == "test_empty_string_returns_none"
    assert failed_run.output["failed_cases"][0]["signature"]

    regression_run = run_tests_tool(task_ctx, "regression")
    assert regression_run.ok and regression_run.output["all_passed"]

    unknown = run_tests_tool(task_ctx, "everything")
    assert not unknown.ok and "unknown test_set" in unknown.error


def test_run_tests_tool_reports_success_after_fix(task_ctx: ToolContext) -> None:
    assert apply_patch(task_ctx, _guard_diff()).ok
    assert run_tests_tool(task_ctx, "all").output["all_passed"]


# ---------- registry 与轨迹 ----------


def test_registry_unknown_tool_and_bad_args(task_ctx: ToolContext) -> None:
    bad = execute(task_ctx, "no_such_tool", {})
    assert not bad.ok and "unknown tool" in bad.error

    bad_args = execute(task_ctx, "read_file", {"offset": 3})  # 缺 path
    assert not bad_args.ok and "bad arguments" in bad_args.error


def test_trajectory_jsonl_format(task_ctx: ToolContext) -> None:
    execute(task_ctx, "list_files", {}, round_no=1, state="LOCALIZE")
    path = task_ctx.tracker.path
    assert path is not None and path.exists()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(events) >= 1
    event = events[-1]
    for key in (
        "event_id",
        "task_id",
        "round",
        "state",
        "tool",
        "input",
        "output_summary",
        "duration_ms",
        "timestamp",
    ):
        assert key in event
    assert event["tool"] == "list_files" and event["round"] == 1 and event["state"] == "LOCALIZE"
