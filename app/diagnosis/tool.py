"""propose_acceptance 工具(G10):Agent 提交验收清单提议的唯一通道。

提议只是候选:平台在冻结快照上重做红/绿基线校验之后,还要 trusted_user 批准才
冻结 manifest(SPEC §7 G10"自动提议不等于可信验收清单")。本工具只做三件事:
按同一套测试身份口径校验语法/重叠、构造提议对象挂到 ctx(由 diagnose 节点取走)、
把"提交了什么"登记成回执。任何批准语义都不在这里发生。
"""

from __future__ import annotations

from typing import Any

from app.context.evidence import EvidenceRef
from app.diagnosis.proposal import AcceptanceProposal
from app.errors import InvalidRequestError
from app.task_spec import MODE_DIAGNOSE_THEN_REPAIR
from app.tools.base import ToolContext, ToolResult


def propose_acceptance(
    ctx: ToolContext,
    failed_tests: list[str],
    regression_tests: list[str],
    rationale: str = "",
    evidence_ids: list[str] | None = None,
) -> ToolResult:
    """提交验收清单提议;仅在 diagnose_then_repair 模式可用。"""
    if ctx.mode != MODE_DIAGNOSE_THEN_REPAIR:
        return ToolResult.fail(
            "propose_acceptance is only available in diagnose_then_repair tasks;"
            " this task's acceptance list is already fixed at intake"
        )
    try:
        proposal = AcceptanceProposal.build(
            task_id=ctx.task_id,
            failed_tests=[str(x) for x in (failed_tests or [])],
            regression_tests=[str(x) for x in (regression_tests or [])],
            rationale=str(rationale or ""),
            evidence_ids=[str(x) for x in (evidence_ids or [])],
            source_snapshot_hash=ctx.source_snapshot_hash,
            task_spec_hash=ctx.task_spec_hash,
        )
    except InvalidRequestError as exc:
        return ToolResult.fail(str(exc))
    ctx.pending_acceptance = proposal
    return ToolResult(
        ok=True,
        output={
            "proposal_id": proposal.proposal_id,
            "content_hash": proposal.content_hash(),
            "failed_tests": list(proposal.failed_tests),
            "regression_tests": list(proposal.regression_tests),
            "note": (
                "proposal recorded; the platform will re-verify red/green baselines on the"
                " frozen snapshot and a trusted user must approve before any patch is produced"
            ),
        },
    )
