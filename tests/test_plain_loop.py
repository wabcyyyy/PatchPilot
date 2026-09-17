"""M3 纯 Python 工具循环测试:FakeLLM 驱动的完整修复闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import BudgetError
from app.gitops.differ import working_tree_diff
from app.gitops.snapshot import create_workspace
from app.graph.plain_loop import run_plain_loop
from app.llm.base import AssistantTurn
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

sys_path = Path(__file__).parent
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]


@pytest.fixture()
def ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="T-LOOP")
    return ToolContext(
        task_id="T-LOOP",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=tracker,
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": REGRESSION_IDS},
    )


def _fix_diff() -> str:
    """在临时副本上生成 demo_repo 的正确修复 diff,喂给 FakeLLM 脚本。"""
    import shutil
    import tempfile

    from app.gitops.testing import materialize_repo

    tmp = Path(tempfile.mkdtemp(prefix="fixdiff-"))
    try:
        repo_src = tmp / "src"
        materialize_repo(sys_path / "fixtures" / "demo_repo", repo_src)
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


def _repair_script() -> list[dict]:
    return [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "apply_patch", "args": {"diff_text": _fix_diff()}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "空字符串未防御,已补早退分支"}},
    ]


def test_full_repair_loop_via_fake_llm(ctx: ToolContext) -> None:
    model = FakeLLM(_repair_script())
    outcome = run_plain_loop(ctx, model, "空字符串抛异常,应返回 None")

    assert outcome.success and outcome.finish_declared and outcome.patch_applied
    assert outcome.turns == len(_repair_script())

    # 工作区确实被修复,且全量测试通过
    target = ctx.workspace / "src" / "dateparse.py"
    assert "value.strip()" in target.read_text(encoding="utf-8")

    # 轨迹完整可解析
    assert ctx.tracker.path is not None
    events = [
        json.loads(line) for line in ctx.tracker.path.read_text(encoding="utf-8").splitlines()
    ]
    tools_used = [e["tool"] for e in events]
    assert tools_used[0] == "search_code" and tools_used[-1] == "finish"
    assert events[-1]["output_summary"]["patch_applied"] is True


def test_loop_finishes_false_when_script_exhausted(ctx: ToolContext) -> None:
    outcome = run_plain_loop(ctx, FakeLLM([{"tool": "list_files", "args": {}}]), "任意 issue")
    assert not outcome.success
    assert "exhausted" in outcome.summary


def test_loop_raises_budget_error_on_endless_content(ctx: ToolContext) -> None:
    class ChattyModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="思考中……", finish_reason="stop")

    with pytest.raises(BudgetError):
        run_plain_loop(ctx, ChattyModel(), "issue", max_turns=3)


def test_loop_raises_budget_error_when_tokens_exhausted(ctx: ToolContext) -> None:
    """累计 token 超预算 → BudgetError,且先于 max_turns 触发。"""

    class SpendthriftModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="x", finish_reason="stop", usage_tokens=100_000)

    with pytest.raises(BudgetError) as exc_info:
        run_plain_loop(ctx, SpendthriftModel(), "issue", max_turns=10, token_budget=50_000)
    assert "exceed budget" in str(exc_info.value)


def test_loop_token_budget_zero_disables_token_check(ctx: ToolContext) -> None:
    """token_budget=0 表示不限制:大用量模型也要到 max_turns 才耗尽。"""

    class SpendthriftModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="x", finish_reason="stop", usage_tokens=100_000)

    with pytest.raises(BudgetError) as exc_info:
        run_plain_loop(ctx, SpendthriftModel(), "issue", max_turns=3, token_budget=0)
    assert "max_turns" in str(exc_info.value)
