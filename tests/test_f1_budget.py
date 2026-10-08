"""S02/F1 端到端:最后一条回复超预算,两引擎都不得 resolved(review F1 复现)。

复现口径与审查探针(audit_probe.py)一致:ExpensiveFinish 把回放最后一步的
usage 抬到 50000,任务预算 20000——旧实现 plain/graph 双双 FINISHED/resolved,
修复后必须 BUDGET_EXCEEDED/failed 且真实用量入账不丢。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.graph.plain_loop import run_plain_loop
from app.graph.resources import EXCEEDED, ResourceLedger
from app.graph.runner import run_task_graph
from app.llm.base import AssistantTurn, ToolCall
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

BUDGET = 20000
BIG = 50000


class ExpensiveFinish(FakeLLM):
    """回放脚本的最后一步报 50000 usage(与审查探针同构)。"""

    def complete(self, messages, tools):
        result = super().complete(messages, tools)
        if self.consumed == len(self.script):
            result.usage_tokens = BIG
            result.completion_tokens = BIG
        return result


@pytest.fixture()
def budget_20000(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATCHPILOT_TOKEN_BUDGET", str(BUDGET))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_graph_last_reply_over_budget_never_resolves(tmp_path: Path, budget_20000) -> None:
    bug = load_bug("BUG-001")
    model = ExpensiveFinish(load_replay_script(bug, kind="graph"))
    result = run_task_graph(bug, model, runs_root=tmp_path / "graph")
    assert result.status == "BUDGET_EXCEEDED", result
    assert result.verdict == "failed"
    assert (result.tokens_used or 0) >= BIG, "最后一条回复的真实用量必须入账"
    assert result.resource_status == EXCEEDED


def test_plain_last_reply_over_budget_never_resolves(tmp_path: Path, budget_20000) -> None:
    bug = load_bug("BUG-001")
    model = ExpensiveFinish(load_replay_script(bug, kind="plain"))
    result = run_task(bug, model, runs_root=tmp_path / "plain")
    assert result.status == "BUDGET_EXCEEDED", result
    assert result.verdict == "failed"
    assert (result.tokens_used or 0) >= BIG
    assert result.resource_status == EXCEEDED


class _OverrunModel:
    """一次回复:多个工具调用 + 巨额 usage(F1:超额回复的工具不得执行)。"""

    provider = "test-stub"

    def __init__(self, *, finish: bool = False, plain_text: bool = False) -> None:
        self.finish = finish
        self.plain_text = plain_text

    def complete(self, messages, tools):
        if self.plain_text:
            return AssistantTurn(
                content="I think I fixed it",
                finish_reason="stop",
                usage_tokens=BIG,
                completion_tokens=BIG,
            )
        if self.finish:
            calls = [
                ToolCall(id="c1", name="finish", arguments={"success": True, "summary": "done"})
            ]
        else:
            calls = [
                ToolCall(id="c1", name="apply_patch", arguments={"patch_text": "x"}),
                ToolCall(id="c2", name="run_tests", arguments={"test_set": "failed"}),
            ]
        return AssistantTurn(
            tool_calls=calls, finish_reason="tool_calls", usage_tokens=BIG, completion_tokens=BIG
        )


def _loop_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        task_id="T-F1",
        workspace=tmp_path / "ws",
        baseline_commit="",
        tracker=Tracker(None, task_id="T-F1"),
        report_dir=tmp_path / "reports",
    )


def test_multi_tool_reply_over_budget_executes_nothing(tmp_path: Path) -> None:
    from app.errors import BudgetError

    ctx = _loop_ctx(tmp_path)
    ledger = ResourceLedger(task_id="T-F1", token_limit=BUDGET, output_reserve=0)
    with pytest.raises(BudgetError, match="exceeded after reply"):
        run_plain_loop(ctx, _OverrunModel(), "issue", max_turns=3, token_budget=0, ledger=ledger)
    tools_executed = [e.tool for e in ctx.tracker.events]
    assert "apply_patch" not in tools_executed and "run_tests" not in tools_executed
    assert "resource_overrun" in tools_executed
    assert ledger.tokens_used == BIG and ledger.resource_status == EXCEEDED


def test_finish_reply_over_budget_never_declares_success(tmp_path: Path) -> None:
    """超额回复里的 finish 不被兑现:模型声明不得替代资源验收。"""
    from app.errors import BudgetError

    ctx = _loop_ctx(tmp_path)
    ledger = ResourceLedger(task_id="T-F1", token_limit=BUDGET, output_reserve=0)
    with pytest.raises(BudgetError, match="exceeded after reply"):
        run_plain_loop(
            ctx, _OverrunModel(finish=True), "issue", max_turns=3, token_budget=0, ledger=ledger
        )
    assert ledger.tokens_used == BIG


def test_plain_text_reply_over_budget_stops_loop(tmp_path: Path) -> None:
    from app.errors import BudgetError

    ctx = _loop_ctx(tmp_path)
    ledger = ResourceLedger(task_id="T-F1", token_limit=BUDGET, output_reserve=0)
    with pytest.raises(BudgetError, match="exceeded after reply"):
        run_plain_loop(
            ctx, _OverrunModel(plain_text=True), "issue", max_turns=3, token_budget=0, ledger=ledger
        )
    assert ledger.tokens_used == BIG


def test_abandoned_call_keeps_usage_and_marks_unknown(tmp_path: Path) -> None:
    """调用中途异常:已入账的真实用量不丢,在途调用标 unknown(不写 0)。"""
    from app.graph.resources import UNKNOWN

    class ExplodingModel:
        provider = "test-stub"

        def complete(self, messages, tools):
            raise RuntimeError("provider 500")

    ctx = _loop_ctx(tmp_path)
    ledger = ResourceLedger(task_id="T-F1", token_limit=BUDGET, output_reserve=0)
    warmup = ledger.begin_call(stage="LOCALIZE")
    ledger.record_usage(warmup, prompt_tokens=500, completion_tokens=100)
    with pytest.raises(RuntimeError):
        run_plain_loop(ctx, ExplodingModel(), "issue", max_turns=2, token_budget=0, ledger=ledger)
    assert ledger.tokens_used == 600  # 已知消耗不蒸发
    assert ledger.resource_status == UNKNOWN
    assert any(c.status == "unknown" for c in ledger.calls.values())
