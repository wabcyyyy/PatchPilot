"""G03 共享原子预留、容量与费用账本(SPEC §5 / §7 G03,验收矩阵 T05、T06)。

这些用例钉的是"额度什么时候被授予、什么时候必须不再授予",不是实现细节:
并发竞态、重复结算、未知消耗的持有、恢复不重授、现金与调用数维度、请求前容量拒绝。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.errors import BudgetError, TaskError
from app.evals.pricing import (
    COST_LOCAL_ESTIMATE,
    COST_PROVIDER_USAGE,
    cost_of_usage,
)
from app.graph.resources import (
    BUDGET_COST,
    BUDGET_HYBRID,
    BUDGET_TOKENS,
    EXCEEDED,
    EXHAUSTED,
    UNKNOWN,
    WITHIN_BUDGET,
    ResourceLedger,
    ledger_from_settings,
    load_ledger_state,
    report_summary,
)
from app.llm.fake import FakeLLM
from app.llm.usage import USAGE_ESTIMATED, USAGE_PROVIDER, UsageEnvelope


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "token_budget": 10_000,
        "llm_max_tokens": 1_000,
        "budget_mode": BUDGET_TOKENS,
        "cost_budget_micro_usd": 0,
        "max_model_calls": 0,
        "model_context_tokens": 0,
        "capacity_margin_tokens": 100,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _settle(ledger: ResourceLedger, *, prompt: int, completion: int, stage: str = "LOCALIZE"):
    call = ledger.begin_call(stage=stage)
    ledger.reserve(call_id=call, stage=stage, request_tokens=prompt, output_reserve=completion)
    return ledger.record_usage(call, prompt_tokens=prompt, completion_tokens=completion)


def test_reserve_counts_every_outstanding_reservation() -> None:
    """T05:两个请求各自都装得下,合计装不下 —— 只允许一次发出。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    first = ledger.begin_call(stage="LOCALIZE")
    ledger.reserve(call_id=first, stage="LOCALIZE", request_tokens=600, output_reserve=0)
    second = ledger.begin_call(stage="LOCALIZE")
    with pytest.raises(BudgetError, match="in flight"):
        ledger.reserve(call_id=second, stage="LOCALIZE", request_tokens=600, output_reserve=0)
    # 估算闸拦下 = exhausted(实际未超);已用的数不被清零
    assert ledger.resource_status == EXHAUSTED
    assert ledger.tokens_used == 0


def test_concurrent_reserves_only_one_wins() -> None:
    """并发竞态必须被同一个临界区挡住,而不是"看起来都行"。

    两个线程都在屏障上等到对方之后才 reserve:此刻两边的预留同时在场,
    600+600 > 1000,必须恰好一个被拒。
    """
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        call = ledger.begin_call(stage="PROPOSE_PATCH")
        barrier.wait()
        try:
            ledger.reserve(call_id=call, stage="PROPOSE_PATCH", request_tokens=600)
        except BudgetError:
            with lock:
                outcomes.append("rejected")
            return
        with lock:
            outcomes.append("granted")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["granted", "rejected"]
    assert ledger.tokens_used == 0  # 还没结算,谁都没花出去
    assert ledger.committed_tokens == 600  # 只有那一个成功的预留占着额度


def test_settled_reservation_frees_its_hold() -> None:
    """结算后预留让位给真实用量:同一个额度不能被预留与已用重复占两次。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.reserve(call_id=call, stage="LOCALIZE", request_tokens=900)
    ledger.record_usage(call, prompt_tokens=100, completion_tokens=50)
    assert ledger.committed_tokens == 150
    again = ledger.begin_call(stage="LOCALIZE")
    ledger.reserve(call_id=again, stage="LOCALIZE", request_tokens=800)  # 150+800 <= 1000
    assert ledger.committed_tokens == 950


def test_duplicate_settle_is_inert_and_recorded() -> None:
    """重复结算:第二次不入账,并把这件事本身留在账本上(幂等性的证据)。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000)
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.record_usage(call, prompt_tokens=30, completion_tokens=20)
    assert ledger.record_usage(call, prompt_tokens=900, completion_tokens=900) is None
    assert ledger.tokens_used == 50
    assert ledger.model_calls == 1
    assert any(a.startswith("duplicate_settle_ignored") for a in ledger.anomalies)


def test_unknown_consumption_keeps_its_reservation() -> None:
    """在途调用崩掉:花费无从知道,但额度**不被还回来**。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    call = ledger.begin_call(stage="PROPOSE_PATCH")
    ledger.reserve(call_id=call, stage="PROPOSE_PATCH", request_tokens=700)
    ledger.abandon_call(call, reason="APIConnectionError")
    assert ledger.resource_status == UNKNOWN
    assert ledger.unknown_held_tokens == 700
    assert ledger.committed_tokens == 700
    with pytest.raises(BudgetError):
        ledger.reserve(
            call_id=ledger.begin_call(stage="VERIFY"),
            stage="VERIFY",
            request_tokens=400,
        )


def test_abandon_message_still_says_without_usage() -> None:
    """历史文案口径保留:F1 的"abandoned without usage"是别的读方在搜的句子。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.reserve(call_id=call, stage="LOCALIZE", request_tokens=100)
    ledger.abandon_call(call, reason="RuntimeError")
    assert "without usage" in (ledger.stop_reason or "")


def test_totals_are_recomputable_from_call_details() -> None:
    """退出条件:主账本可由调用明细重算,不一致就是账本坏了。"""
    ledger = ResourceLedger(task_id="T", token_limit=10_000, price_model="gpt-4o-mini")
    _settle(ledger, prompt=100, completion=40)
    _settle(ledger, prompt=200, completion=10, stage="PROPOSE_PATCH")
    assert ledger.is_consistent()
    rec = ledger.recomputed()
    assert rec["tokens_used"] == ledger.tokens_used == 350
    assert rec["model_calls"] == 2
    assert rec["cost_micro_usd"] == ledger.cost_micro_usd > 0
    assert rec["stage_usage"] == {"LOCALIZE": 140, "PROPOSE_PATCH": 210}


def test_cost_uses_cache_categories_without_double_counting() -> None:
    """缓存命中按子集切价,总额不因 cached 而变多。"""
    full = UsageEnvelope(prompt_tokens=1000, completion_tokens=0, usage_source=USAGE_PROVIDER)
    cached = UsageEnvelope(
        prompt_tokens=1000,
        completion_tokens=0,
        cached_tokens=800,
        usage_source=USAGE_PROVIDER,
    )
    cheap = cost_of_usage("gpt-4o-mini", cached)
    plain = cost_of_usage("gpt-4o-mini", full)
    assert cheap is not None and plain is not None
    # 内置价目不声明缓存档 → 缓存部分按未缓存价全额计,宁可高估
    assert cheap.micro_usd == plain.micro_usd
    assert cheap.cache_pricing == "undiscounted"
    assert cheap.basis == COST_PROVIDER_USAGE


def test_cost_basis_follows_usage_source() -> None:
    """估出来的 token 只能配"估算费用",不许冒充 provider 折价。"""
    record = cost_of_usage(
        "gpt-4o",
        UsageEnvelope(prompt_tokens=10, completion_tokens=10, usage_source=USAGE_ESTIMATED),
    )
    assert record is not None and record.basis == COST_LOCAL_ESTIMATE
    assert record.price_version and record.currency == "USD"


def test_ledger_accrues_money_in_minor_units() -> None:
    """现金维度:micro-USD 整数累计;实际回复越过上限时如实记 cost_overrun。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=0,
        budget_mode=BUDGET_COST,
        cost_limit_micro_usd=3_000_000,
        price_model="gpt-4o",
        output_reserve=0,
    )
    call = ledger.begin_call(stage="LOCALIZE")
    # 预留 1M 未缓存输入 @ $2.50/1M = 2.5e6 micro-USD,上限 3e6 装得下
    ledger.reserve(call_id=call, stage="LOCALIZE", request_tokens=1_000_000, output_reserve=0)
    # 实际回复再带 100k 输出 @ $10/1M = 1e6 micro → 合计 3.5e6,越过上限
    ledger.record_usage(call, prompt_tokens=1_000_000, completion_tokens=100_000)
    assert ledger.cost_micro_usd == 3_500_000
    assert ledger.cost_by_basis[COST_PROVIDER_USAGE] == 3_500_000
    assert ledger.resource_status == EXCEEDED
    assert ledger.cost_overrun is not None
    assert ledger.cost_overrun.cost_limit_micro_usd == 3_000_000


def test_cost_gate_refuses_an_unaffordable_request_before_sending() -> None:
    """现金闸拦在发出之前:预留就装不下,不会"先发出去再说"。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=0,
        budget_mode=BUDGET_COST,
        cost_limit_micro_usd=1_000_000,
        price_model="gpt-4o",
        output_reserve=0,
    )
    with pytest.raises(BudgetError, match="cost budget"):
        ledger.reserve(
            call_id=ledger.begin_call(stage="LOCALIZE"),
            stage="LOCALIZE",
            request_tokens=1_000_000,  # 2.5e6 micro > 1e6 上限
        )
    assert ledger.resource_status == EXHAUSTED


def test_cost_mode_refuses_when_price_is_unknown() -> None:
    """要按现金闸却算不出价目:这次请求不能发,不是"免检放行"。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=0,
        budget_mode=BUDGET_COST,
        cost_limit_micro_usd=5_000,
        price_model="",  # 无价目
        output_reserve=0,
    )
    with pytest.raises(BudgetError, match="known price"):
        ledger.reserve(
            call_id=ledger.begin_call(stage="LOCALIZE"), stage="LOCALIZE", request_tokens=10
        )


def test_call_limit_stops_before_the_request() -> None:
    """调用数是一类独立资源,不能只靠 token 数间接挡。"""
    ledger = ResourceLedger(task_id="T", token_limit=0, call_limit=2, output_reserve=0)
    _settle(ledger, prompt=10, completion=5)
    _settle(ledger, prompt=10, completion=5)
    with pytest.raises(BudgetError, match="model-call limit"):
        ledger.reserve(
            call_id=ledger.begin_call(stage="LOCALIZE"), stage="LOCALIZE", request_tokens=10
        )


def test_capacity_refuses_before_sending() -> None:
    """T12 的账本侧:越过声明容量在**请求前**拒绝,不宣称支持更大的窗口。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=0,
        model_capacity_tokens=8_000,
        capacity_margin_tokens=1_000,
        output_reserve=500,
    )
    call = ledger.begin_call(stage="PROPOSE_PATCH")
    with pytest.raises(BudgetError, match="declared model capacity"):
        ledger.reserve(call_id=call, stage="PROPOSE_PATCH", request_tokens=8_000)
    assert ledger.over_capacity_requests == 1
    assert ledger.resource_status == WITHIN_BUDGET  # 总额没超,是这一发装不下


def test_hybrid_mode_keeps_both_gates() -> None:
    """hybrid 不静默取消旧 token 闸(§5 的"不能盲目继承")。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=500,
        budget_mode=BUDGET_HYBRID,
        cost_limit_micro_usd=10**9,
        price_model="gpt-4o-mini",
        output_reserve=0,
    )
    call = ledger.begin_call(stage="LOCALIZE")
    with pytest.raises(BudgetError, match="token budget"):
        ledger.reserve(call_id=call, stage="LOCALIZE", request_tokens=900)
    assert ledger.resource_status == EXHAUSTED


def test_ledger_state_round_trip_keeps_unknown_holdings(tmp_path: Path) -> None:
    """恢复预留:落盘/读回的账本必须带着未知持有与上限回来,额度不重授。"""
    ledger = ResourceLedger(
        task_id="T",
        token_limit=1000,
        output_reserve=0,
        budget_mode=BUDGET_TOKENS,
        state_path=tmp_path / "ledger_state.json",
    )
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.reserve(call_id=call, stage="LOCALIZE", request_tokens=600)
    ledger.abandon_call(call, reason="crash")

    saved = json.loads((tmp_path / "ledger_state.json").read_text(encoding="utf-8"))
    restored = ResourceLedger.from_state(saved)
    assert restored.unknown_held_tokens == 600
    assert restored.committed_tokens == 600
    assert restored.token_limit == 1000
    with pytest.raises(BudgetError):
        restored.reserve(
            call_id=restored.begin_call(stage="PROPOSE_PATCH"),
            stage="PROPOSE_PATCH",
            request_tokens=500,
        )


def test_ledger_file_is_reused_by_the_same_task(tmp_path: Path) -> None:
    """同一 run_dir 重建账本走快照,而不是"账本又是新的一份"。"""
    settings = _settings(token_budget=1000)
    first = ledger_from_settings(task_id="T", settings=settings, run_dir=tmp_path)
    call = first.begin_call(stage="LOCALIZE")
    first.record_usage(call, prompt_tokens=400, completion_tokens=100)

    again = ledger_from_settings(task_id="T", settings=settings, run_dir=tmp_path)
    assert again.tokens_used == 500
    assert again.model_calls == 1
    assert again.resource_status == WITHIN_BUDGET


def test_corrupt_ledger_state_cannot_regrant_or_certify(tmp_path: Path) -> None:
    """坏快照保留未知消费,不当成新任务重授额度;读取失败不伪造实际用量。"""
    (tmp_path / "ledger_state.json").write_text("{not json", encoding="utf-8")
    ledger = ledger_from_settings(
        task_id="T", settings=_settings(token_budget=1000), run_dir=tmp_path
    )
    assert ledger.tokens_used == 0
    assert load_ledger_state(tmp_path) is None
    assert ledger.resource_status == UNKNOWN
    with pytest.raises(BudgetError, match="persisted ledger unreadable"):
        ledger.reserve(call_id="untrusted", request_tokens=100, stage="PROPOSE_PATCH")
    from app.graph.acceptance import final_acceptance

    decision = final_acceptance(
        verify_failed_ok=True,
        verify_regression_ok=True,
        verify_ran=True,
        gate_ok=True,
        gate_ran=True,
        diff_non_empty=True,
        ledger=ledger,
    )
    assert not decision.resolved
    assert (tmp_path / "ledger_state.json").read_text(encoding="utf-8") == "{not json"


def test_report_summary_is_redacted_and_consistent() -> None:
    """报告摘要带全部维度,但不带调用明细原文(T24),并自证一致性。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, price_model="gpt-4o-mini")
    _settle(ledger, prompt=100, completion=20)
    summary = report_summary(ledger)
    assert summary["calls_consistent"] is True
    assert summary["budget_mode"] == BUDGET_TOKENS
    assert summary["limits"]["token_limit"] == 1000
    assert summary["cost_micro_usd"] > 0
    assert "calls" not in summary
    assert report_summary(None)["resource_status"] == UNKNOWN


def test_validate_budget_policy_rejects_unpriced_online_task() -> None:
    """没有已知价格与金额上限的 cost 任务在**受理处**拒绝,不发请求。"""
    from app.task_spec import validate_budget_policy

    with pytest.raises(TaskError, match="cost_budget_micro_usd"):
        validate_budget_policy(
            _settings(budget_mode=BUDGET_COST, cost_budget_micro_usd=0),
            "gpt-4o-mini",
            online=True,
        )
    with pytest.raises(TaskError, match="no known price"):
        validate_budget_policy(
            _settings(budget_mode=BUDGET_COST, cost_budget_micro_usd=100),
            "some-unlisted-model",
            online=True,
        )
    # tokens 模式与离线回放都不受影响(零花费路径不能被价表挡死)
    validate_budget_policy(_settings(), "gpt-4o-mini", online=True)
    validate_budget_policy(_settings(budget_mode=BUDGET_COST), "x", online=False)


def test_stage_switch_does_not_regrant_budget() -> None:
    """阶段切换不是新预算:跨阶段累计与预留基数连续。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=0)
    _settle(ledger, prompt=300, completion=100, stage="LOCALIZE")
    _settle(ledger, prompt=200, completion=100, stage="PROPOSE_PATCH")
    assert ledger.tokens_used == 700
    with pytest.raises(BudgetError):
        ledger.reserve(
            call_id=ledger.begin_call(stage="VERIFY"), stage="VERIFY", request_tokens=400
        )


def test_losing_candidate_consumption_stays_counted() -> None:
    """落选候选的消耗一样入账:一个任务只有一份账本,候选不各记各的。"""
    ledger = ResourceLedger(task_id="T", token_limit=0, output_reserve=0)
    for index in range(2):
        _settle(ledger, prompt=200, completion=50, stage=f"BRANCH-{index}")
    assert ledger.model_calls == 2
    assert ledger.stage_usage == {"BRANCH-0": 250, "BRANCH-1": 250}
    assert ledger.tokens_used == 500


def test_output_reserve_shortage_is_recorded_not_claimed() -> None:
    """输出预留为 0 = 无法预留:能力边界如实留下,不宣称绝不超支。"""
    ledger = ResourceLedger(task_id="T", token_limit=100, output_reserve=0)
    ledger.ensure_request_fits(10)
    assert ledger.output_reserve_unavailable is True


# ---------- 循环接线:预留/结算/落盘真的发生在引擎里 ----------


def _loop_ctx(tmp_path: Path, task_id: str = "T-RESV"):
    from app.tools.base import ToolContext
    from app.tools.tracker import Tracker

    return ToolContext(
        task_id=task_id,
        workspace=tmp_path / "ws",
        baseline_commit="",
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id=task_id),
        report_dir=tmp_path / "reports",
    )


class _CancelDuringReplyModel:
    """回复已经到手,取消才落下:这一次花费要结算,但一个工具都不许执行。"""

    provider = "stub"

    def __init__(self, event: threading.Event) -> None:
        from app.llm.base import AssistantTurn, ToolCall

        self.event = event
        self.turn = AssistantTurn(
            tool_calls=[ToolCall(id="c1", name="list_files", arguments={})],
            finish_reason="tool_calls",
            usage_tokens=77,
            prompt_tokens=50,
            completion_tokens=27,
        )

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.event.set()  # 模拟"回复晚到,而取消已在此期间发生"
        return self.turn


def test_late_reply_is_settled_but_no_tool_runs(tmp_path: Path) -> None:
    """G03 的取消口径:晚到的回复只计量不行动,不是"当成没发生"。"""
    from app.errors import TaskCancelled
    from app.graph.plain_loop import run_plain_loop

    event = threading.Event()
    ctx = _loop_ctx(tmp_path)
    ledger = ResourceLedger(task_id=ctx.task_id, token_limit=0, output_reserve=0)

    with pytest.raises(TaskCancelled, match="after reply"):
        run_plain_loop(
            ctx,
            _CancelDuringReplyModel(event),
            "issue",
            max_turns=3,
            ledger=ledger,
            cancel_event=event,
        )
    assert ledger.tokens_used == 77, "这一次请求真的花掉了钱,必须入账"
    assert ledger.resource_status == WITHIN_BUDGET

    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    blocked = next(e for e in events if e["tool"] == "list_files")
    assert blocked["input"]["blocked"] is True, "只留一条被取消挡下的取证事件"
    assert blocked["error"] == "task cancelled before this tool ran"
    assert not (tmp_path / "ws" / "src").exists(), "取消后不得留下工具副作用"


def test_loop_persists_ledger_state_for_resume(tmp_path: Path) -> None:
    """恢复不重授的前提:引擎真的把账本落到 run_dir,而不只留在内存里。"""
    from app.graph.plain_loop import run_plain_loop
    from app.llm.base import AssistantTurn

    ctx = _loop_ctx(tmp_path, "T-RESV-FILE")
    state_path = tmp_path / "ledger_state.json"
    ledger = ResourceLedger(
        task_id=ctx.task_id, token_limit=0, output_reserve=0, state_path=state_path
    )
    model = SimpleNamespace(
        provider="stub",
        complete=lambda messages, tools: AssistantTurn(
            content="好了", finish_reason="stop", usage_tokens=40, completion_tokens=40
        ),
    )
    run_plain_loop(ctx, model, "issue", max_turns=2, ledger=ledger, complete_on_text=True)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["tokens_used"] == 40
    assert saved["version"] == 1
    assert all(r["state"] != "reserved" for r in saved["reservations"])


_LOCALIZE_SCRIPT = [
    {"tool": "search_code", "args": {"keyword": "parse_date"}},
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "finish", "args": {"success": True, "summary": "根因:parse_date 未处理空白字符串"}},
]


class _ProcessDied(BaseException):
    """进程死亡的代理异常:必须绕过 `except Exception`,才能造出"节点被打断"的形态
    (与 tests/test_resume.py 的同名代理同理)。"""


class _DieMidRequest(FakeLLM):
    """回放前 limit 步,之后的请求死在模型调用里:那一次的花费无从知道。"""

    def __init__(self, script: list[dict[str, Any]], limit: int) -> None:
        super().__init__(script)
        self._limit = limit
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls > self._limit:
            raise _ProcessDied("process died inside the model request")
        return super().complete(messages, tools)


def test_midrequest_crash_resume_cannot_certify(tmp_path: Path) -> None:
    """§3.1 + G03:进程死在请求中间 → 恢复带着 unknown 回来,不得被判成 resolved。

    旧形状是"恢复后账本全新",于是崩溃前那次可能已经计费、却无从知道花多少的请求
    被抹成"没发生",续跑照样能宣称所有资源限制通过。现在未知消耗继续占额度,
    资源维度保持 unknown,验收按原有严格语义拒绝/待审。
    """
    from app.evals.bugset import load_bug, load_replay_script
    from app.graph.runner import run_task_graph

    bug = load_bug("BUG-001")
    task_id = "T-RESUME-UNKNOWN"
    run_dir = tmp_path / "runs" / task_id
    with pytest.raises(_ProcessDied):
        run_task_graph(
            bug,
            _DieMidRequest([*_LOCALIZE_SCRIPT, {"tool": "list_files", "args": {}}], 5),
            runs_root=tmp_path / "runs",
            task_id=task_id,
            run_dir=run_dir,
        )

    saved = json.loads((run_dir / "ledger_state.json").read_text(encoding="utf-8"))
    assert saved["unknown_held_tokens"] > 0, "在途请求的预留必须留下"
    assert saved["resource_status"] == UNKNOWN

    second = run_task_graph(
        bug,
        FakeLLM(load_replay_script(bug, kind="graph")),
        runs_root=tmp_path / "runs",
        task_id=task_id,
        run_dir=run_dir,
        resume=True,
    )
    assert second.verdict != "resolved", "资源未知即不得判成功(§3.1 的严格语义)"
    assert second.resource_status == UNKNOWN
    assert second.resources["unknown_held_tokens"] == saved["unknown_held_tokens"]
