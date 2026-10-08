"""S02/F1:共享终局验收的判定矩阵(ADR-0009 §1)。

graph 的 finish 节点与 plain 的判定段调用同一个 final_acceptance;
两引擎对"相同补丁/测试/策略"的终局结论必须一致。
"""

from __future__ import annotations

from app.graph.acceptance import (
    VALIDATION_INCONCLUSIVE,
    VALIDATION_PASSED,
    final_acceptance,
)
from app.graph.resources import EXCEEDED, UNKNOWN, WITHIN_BUDGET, ResourceLedger


def _within_ledger() -> ResourceLedger:
    return ResourceLedger(task_id="T", token_limit=1000)


def _green_kwargs() -> dict:
    return {
        "verify_failed_ok": True,
        "verify_regression_ok": True,
        "verify_ran": True,
        "gate_ok": True,
        "gate_ran": True,
        "diff_non_empty": True,
    }


def test_all_green_within_budget_resolves() -> None:
    decision = final_acceptance(ledger=_within_ledger(), **_green_kwargs())
    assert decision.resolved is True
    assert decision.validation_status == VALIDATION_PASSED
    assert decision.gate_status == "passed"
    assert decision.resource_status == WITHIN_BUDGET
    assert decision.reasons == []


def test_verification_passed_but_budget_exceeded_never_resolves() -> None:
    """ADR-0009 §1:即使验证通过,也不能静默写 resolved;validation 仍如实记 passed。"""
    ledger = _within_ledger()
    call = ledger.begin_call(stage="PROPOSE_PATCH")
    ledger.record_usage(call, prompt_tokens=0, completion_tokens=5000)  # limit 1000
    decision = final_acceptance(ledger=ledger, **_green_kwargs())
    assert decision.resolved is False
    assert decision.validation_status == VALIDATION_PASSED
    assert decision.resource_status == EXCEEDED
    assert any("resource_status=exceeded" in r for r in decision.reasons)


def test_unknown_resource_never_resolves() -> None:
    ledger = _within_ledger()
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.abandon_call(call, reason="ConnectionError")
    decision = final_acceptance(ledger=ledger, **_green_kwargs())
    assert decision.resolved is False
    assert decision.resource_status == UNKNOWN


def test_missing_ledger_is_unknown_not_within() -> None:
    """无账本按 unknown 处理,不冒充 within_budget。"""
    decision = final_acceptance(ledger=None, **_green_kwargs())
    assert decision.resolved is False
    assert decision.resource_status == "unknown"


def test_empty_diff_never_resolves() -> None:
    decision = final_acceptance(
        ledger=_within_ledger(), **{**_green_kwargs(), "diff_non_empty": False}
    )
    assert decision.resolved is False
    assert any("empty" in r for r in decision.reasons)


def test_cancelled_never_resolves() -> None:
    decision = final_acceptance(ledger=_within_ledger(), **{**_green_kwargs(), "cancelled": True})
    assert decision.resolved is False
    assert any("cancel" in r for r in decision.reasons)


def test_gate_rejected_never_resolves() -> None:
    decision = final_acceptance(ledger=_within_ledger(), **{**_green_kwargs(), "gate_ok": False})
    assert decision.resolved is False
    assert decision.gate_status == "rejected"


def test_double_run_inconsistent_is_inconclusive() -> None:
    decision = final_acceptance(
        ledger=_within_ledger(), **{**_green_kwargs(), "double_run_inconsistent": True}
    )
    assert decision.resolved is False
    assert decision.validation_status == VALIDATION_INCONCLUSIVE


def test_verification_not_run_never_resolves() -> None:
    decision = final_acceptance(
        ledger=_within_ledger(),
        **{
            **_green_kwargs(),
            "verify_ran": False,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
        },
    )
    assert decision.resolved is False
    assert decision.validation_status == "not_run"


def test_same_facts_same_decision_for_both_engines() -> None:
    """graph 与 plain 传入相同事实 → 相同结论(验收面同源)。"""
    a = final_acceptance(ledger=_within_ledger(), **_green_kwargs())
    b = final_acceptance(ledger=_within_ledger(), **_green_kwargs())
    assert (a.resolved, a.validation_status, a.gate_status, a.resource_status) == (
        b.resolved,
        b.validation_status,
        b.gate_status,
        b.resource_status,
    )


def test_resumed_state_tokens_guard_against_ledger_undercount() -> None:
    """恢复场景:账本只记续跑后的调用,state 累计量更大时取大复核,不给 resolved。"""
    from app.graph.acceptance import acceptance_from_state

    ledger = ResourceLedger(task_id="T", token_limit=1000)  # 续跑后账本为空
    state = {
        "verify_failed_ok": True,
        "verify_regression_ok": True,
        "gate_violations": [],
        "tokens_used": 5000,
    }
    decision = acceptance_from_state(state, ledger=ledger, diff_non_empty=True)
    assert decision.resolved is False
    assert decision.resource_status == "exceeded"
    assert any("state/ledger max" in r for r in decision.reasons)
