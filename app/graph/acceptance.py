"""共享终局验收(S02/F1,ADR-0009 §1/§4)。

缺陷现场(review F1/P1):graph 的 finish 节点无条件写 FINISHED/resolved,
plain 的判定段只看两个布尔;两引擎都不在终局复查"未超预算、非空 diff、取消状态"。
修法:graph 与 plain 调用**同一个** acceptance helper,终局判定检查:

    当前非空 diff ∧ 完整测试身份(verify 布尔来自真实 junit)∧ 门禁通过
    ∧ 双跑一致(开启时)∧ 资源 within_budget ∧ 未取消 ⇒ resolved

不只相信 state 里的两个 True;预算超限仍 BUDGET_EXCEEDED/failed(旧字段保留),
本模块不新增"超预算也 resolved"的模式。

`double_run_mismatch` 从 nodes.py 迁入(两引擎同一条比对代码);verify 节点与
plain 驱动器都遵守 Settings.verify_double_run——比较臂不允许少跑复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.adapters.pytest_adapter import PytestReport
    from app.graph.resources import ResourceLedger

# validation_status / gate_status 取值(spec §3.1)
VALIDATION_NOT_RUN = "not_run"
VALIDATION_PASSED = "passed"
VALIDATION_FAILED = "failed"
VALIDATION_INCONCLUSIVE = "inconclusive"

GATE_NOT_RUN = "not_run"
GATE_PASSED = "passed"
GATE_REJECTED = "rejected"
GATE_INCONCLUSIVE = "inconclusive"


def junit_case_ids(report: PytestReport) -> set[tuple[str, str, str]]:
    """junit 两次运行比对用的测试 id 集合(file, classname, name 三元组,不含判定)。"""
    return {(file_attr, classname, name) for file_attr, classname, name, _ in report.case_results}


def double_run_mismatch(
    first_failed: PytestReport,
    first_reg: PytestReport,
    rerun_failed: PytestReport,
    rerun_reg: PytestReport,
) -> str:
    """比对 verify 双跑的两次 junit;一致返回空串,不一致返回不匹配原因。

    一致 = 两个测试集各自满足:两次收集到的测试 id 集合相等,且第一遍
    (全绿)通过的 id 在第二遍无任何非 passed 记录(rerun 亦须 all_passed)。
    """
    for label, first, rerun in (
        ("failed", first_failed, rerun_failed),
        ("regression", first_reg, rerun_reg),
    ):
        if junit_case_ids(first) != junit_case_ids(rerun):
            return f"{label}: junit test id set differs between runs"
        if not rerun.all_passed:
            return f"{label}: rerun not all passed (first run was)"
    return ""


@dataclass
class AcceptanceDecision:
    """终局验收的结构化结论;reasons 供报告/轨迹给出"为什么不是 resolved"。"""

    resolved: bool
    validation_status: str = VALIDATION_NOT_RUN
    gate_status: str = GATE_NOT_RUN
    resource_status: str = "unknown"
    reasons: list[str] = field(default_factory=list)


def final_acceptance(
    *,
    verify_failed_ok: bool,
    verify_regression_ok: bool,
    verify_ran: bool,
    gate_ok: bool,
    gate_ran: bool,
    diff_non_empty: bool,
    double_run_inconsistent: bool = False,
    cancelled: bool = False,
    ledger: ResourceLedger | None = None,
) -> AcceptanceDecision:
    """终局共享验收。任何一条不满足都不是 resolved,并给出原因清单。

    - 资源读账本快照:exceeded/exhausted/unknown 一律不给 resolved
      (ADR-0009 §1:即使验证通过,也不能静默写 resolved);
    - 无账本(极老调用方)按 unknown 处理,不冒充 within_budget;
    - 取消置真:终局拒绝(engine 侧与 service 的 CANCELLED 终态口径一致);
    - 空 diff = "没有补丁却宣称验证过",直接拒绝;
    - 双跑不一致 = inconclusive(调用方收敛 NEEDS_REVIEW,与现语义一致)。
    """
    status = ledger.snapshot() if ledger is not None else {}
    resource_status = str(status.get("resource_status", "unknown"))
    reasons: list[str] = []
    validation = VALIDATION_FAILED
    if not verify_ran:
        validation = VALIDATION_NOT_RUN
    elif double_run_inconsistent:
        validation = VALIDATION_INCONCLUSIVE
    elif verify_failed_ok and verify_regression_ok:
        validation = VALIDATION_PASSED
    gate = GATE_PASSED if (gate_ran and gate_ok) else (GATE_REJECTED if gate_ran else GATE_NOT_RUN)

    if not verify_ran:
        reasons.append("verification did not run")
    elif double_run_inconsistent:
        reasons.append("verify double-run mismatch")
    elif validation != VALIDATION_PASSED:
        reasons.append("failed/regression test set not fully passed")
    if gate == GATE_REJECTED:
        reasons.append("quality gates rejected the patch")
    if not diff_non_empty:
        reasons.append("workspace diff is empty (no candidate patch)")
    if resource_status != "within_budget":
        reasons.append(f"resource_status={resource_status}")
        if status.get("stop_reason"):
            reasons.append(str(status["stop_reason"]))
    if cancelled:
        reasons.append("task cancelled")

    resolved = (
        validation == VALIDATION_PASSED
        and gate == GATE_PASSED
        and diff_non_empty
        and resource_status == "within_budget"
        and not cancelled
    )
    return AcceptanceDecision(
        resolved=resolved,
        validation_status=validation,
        gate_status=gate,
        resource_status=resource_status,
        reasons=reasons,
    )


def acceptance_from_state(
    state: dict[str, Any],
    *,
    ledger: ResourceLedger | None,
    diff_non_empty: bool,
    cancelled: bool = False,
) -> AcceptanceDecision:
    """graph finish 节点的入口:state 中的 verify/门禁结论 + 当前工作区事实。

    恢复场景的防绕过:账本只记**续跑之后**的调用,检查点 state 里的累计量可能
    更大——两者取大再对任务总额复核,恢复路径不得借"账本是新的"绕过资源契约。
    """
    from app.config import get_settings

    decision = final_acceptance(
        verify_failed_ok=bool(state.get("verify_failed_ok")),
        verify_regression_ok=bool(state.get("verify_regression_ok")),
        verify_ran=bool(
            state.get("verify_failed_ok") is not None
            and state.get("verify_regression_ok") is not None
        ),
        gate_ok=not state.get("gate_violations"),
        gate_ran=True,
        diff_non_empty=diff_non_empty,
        cancelled=cancelled,
        ledger=ledger,
    )
    # 任务总额的权威读数:账本在任务启动时按 Settings 固化(token_limit),
    # 恢复/后续验收都认它;无账本时才退回当前 Settings
    budget = (
        ledger.token_limit
        if ledger is not None
        else int(getattr(get_settings(), "token_budget", 0) or 0)
    )
    if budget > 0:
        state_tokens = int(state.get("tokens_used", 0) or 0)
        ledger_tokens = ledger.tokens_used if ledger is not None else 0
        effective = max(state_tokens, ledger_tokens)
        if effective > budget and decision.resource_status == "within_budget":
            decision.resource_status = "exceeded"
            decision.resolved = False
            decision.reasons.append(
                f"task tokens {effective} exceed budget {budget} (state/ledger max)"
            )
    return decision
