"""变更类工具:apply_patch / git_diff / reset_workspace。

apply_patch 是整个平台的安全要害:先过静态门禁(测试文件/路径/范围),
再过 `git apply --check`,两道都通过才落盘。
"""

from __future__ import annotations

import logging

from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch as git_apply_patch
from app.gitops.rollback import reset_workspace
from app.graph.gates import run_gates
from app.tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)


def git_diff(ctx: ToolContext) -> ToolResult:
    """查看当前工作区相对基线的全部改动(只读)。"""
    diff = working_tree_diff(ctx.workspace)
    return ToolResult(
        ok=True,
        output={
            "changed_files": diff.changed_files,
            "is_empty": diff.is_empty,
            "diff": diff.diff_text
            if len(diff.diff_text) <= 20_000
            else diff.diff_text[:20_000] + "\n... (truncated)",
        },
    )


def apply_patch(ctx: ToolContext, diff_text: str) -> ToolResult:
    """应用 unified diff:静态门禁 → git apply --check → 应用。"""
    gate = run_gates(
        diff_text,
        allowed_paths=ctx.allowed_paths,
        max_files=ctx.max_patch_files,
        forbid_test_files=ctx.forbid_test_files,
    )
    if not gate:
        return ToolResult.fail("; ".join(str(v) for v in gate.violations))

    result = git_apply_patch(ctx.workspace, diff_text)
    if not result.applied:
        return ToolResult.fail(f"git apply --check failed: {result.detail}")

    diff = working_tree_diff(ctx.workspace)
    return ToolResult(
        ok=True,
        output={"applied": True, "changed_files": diff.changed_files},
    )


def reset_to_baseline(ctx: ToolContext) -> ToolResult:
    """把工作区恢复到基线快照(丢弃全部改动)。"""
    reset_workspace(ctx.workspace, ctx.baseline_commit)
    return ToolResult(ok=True, output={"reset_to": ctx.baseline_commit})
