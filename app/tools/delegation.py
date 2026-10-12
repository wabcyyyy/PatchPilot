"""G13:delegate 工具——主线把调查/评审/候选修复委派给子执行者(SPEC §G13)。

handler 只做三件事:校验句柄存在且开启、调 SubtaskManager.delegate、把有界的
ChildResult 折回 ToolResult。子任务真正的名额/额度/执行都在 manager 里;这里
不缓存任何状态,一次 delegate 调用 = 一次同步子任务执行。

不在此 import app.agent.subtasks:registry 在模块加载期 import 本文件,而
subtasks → roles → registry 会形成环;延迟到 handler 内导入,运行时注册表
已完整加载。
"""

from __future__ import annotations

from app.tools.base import ToolContext, ToolResult


def delegate(
    ctx: ToolContext,
    *,
    role: str,
    task: str,
    evidence_ids: list[str] | None = None,
    candidate_id: str | None = None,
) -> ToolResult:
    """把一件事委派给指定角色的子执行者,同步等待其有界结论。

    - role ∈ {researcher, reviewer, repair_candidate}(schema 枚举,越权名当场拒);
    - task 是精简委派说明;evidence_ids 引用父任务已登记的证据(EvidenceRef);
    - candidate_id 用于 reviewer/repair_candidate 指认要评审的候选。
    返回结构化 ChildResult(结论/证据引用/未验证假设/风险/计量/终止原因);
    被拒绝(预算不足/名额满/未配置)也是合法输出,不把工具标成异常让模型重试。
    """
    manager = getattr(ctx, "subtasks", None)
    if manager is None or not getattr(manager, "enabled", False):
        return ToolResult.fail("delegation is not enabled for this task")
    from app.agent.subtasks import SubtaskManager  # 延迟导入,避免注册表↔子任务环

    if not isinstance(manager, SubtaskManager):
        return ToolResult.fail("delegation context is not a SubtaskManager")
    try:
        child = manager.delegate(
            role=role,
            task=task,
            evidence_ids=list(evidence_ids or []),
            candidate_id=candidate_id,
            ctx=ctx,
        )
        return ToolResult(ok=True, output=child.as_dict())
    except Exception as exc:
        return ToolResult.fail(f"{type(exc).__name__}: {exc}")
