"""变更类工具:apply_patch / git_diff / reset_workspace。

apply_patch 是整个平台的安全要害。它是**单入口**的:模型只能提交 Codex 风格的
块协议(patch_text),工具内编译成 unified diff——不做"看首行猜格式"的双格式分派,
那是攻击面。编译产物之后仍是内部唯一事实源,防线链一字不动:
静态门禁(测试文件/路径/影子/范围) → `git apply --check` → 应用 → Python 语法预检。

门禁跑两次:编译前对"仅段头"的骨架 diff 判一次(路径/测试文件/影子/范围是纯表头
信息,不该被锚定失败遮蔽),编译后再对真实产物判一次(与图节点 apply() 复核同源)。
"""

from __future__ import annotations

import logging

from app.gitops.blockpatch import (
    BlockPatchError,
    BlockSection,
    compile_block_patch,
    parse_block_patch,
)
from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch as git_apply_patch
from app.gitops.rollback import reset_workspace
from app.graph.gates import run_gates
from app.tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)


def _block_rejected(exc: BlockPatchError) -> str:
    """协议级拒绝沿用门禁词表([format]/[paths]/[context]),让反馈可比对。"""
    return f"patch rejected ([{exc.tag}] {exc.reason}): {exc.detail}"


def _gate_skeleton(sections: list[BlockSection]) -> str:
    """块段 → 只有段头、没有 hunk 的骨架 diff(仅供编译前的静态门禁读路径/模式)。"""
    parts: list[str] = []
    for section in sections:
        path = section.path
        head = f"diff --git a/{path} b/{path}\n"
        if section.action == "add":
            parts.append(head + f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n")
        elif section.action == "delete":
            parts.append(head + f"deleted file mode 100644\n--- a/{path}\n+++ /dev/null\n")
        else:
            parts.append(head + f"--- a/{path}\n+++ b/{path}\n")
    return "".join(parts)


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


def apply_patch(ctx: ToolContext, patch_text: str) -> ToolResult:
    """应用块协议补丁:解析 → 门禁(骨架) → 编译 → 门禁(产物) → git apply → 语法预检。

    任何一步拒绝都返回带拒绝原因的失败信息,交由模型在下一轮修正;协议级拒绝
    与门禁拒绝共用 `[gate]` 词表,模型不必猜是哪一道拦的。
    """
    try:
        sections = parse_block_patch(patch_text)
    except BlockPatchError as exc:
        return ToolResult.fail(_block_rejected(exc))

    gate = run_gates(
        _gate_skeleton(sections),
        allowed_paths=ctx.allowed_paths,
        max_files=ctx.max_patch_files,
        forbid_test_files=ctx.forbid_test_files,
    )
    if not gate:
        return ToolResult.fail("; ".join(str(v) for v in gate.violations))

    try:
        diff_text = compile_block_patch(ctx.workspace, sections)
    except BlockPatchError as exc:
        return ToolResult.fail(_block_rejected(exc))

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
        # 只有"真应用失败"计入分支信号;门禁/协议拒绝在上面已返回,不计数
        ctx.patch_fail_streak += 1
        return ToolResult.fail(f"patch rejected ({result.rejected_reason}): {result.detail}")
    ctx.patch_fail_streak = 0

    diff = working_tree_diff(ctx.workspace)
    return ToolResult(
        ok=True,
        output={"applied": True, "changed_files": diff.changed_files},
    )


def reset_to_baseline(ctx: ToolContext) -> ToolResult:
    """把工作区恢复到基线快照(丢弃全部改动)。"""
    reset_workspace(ctx.workspace, ctx.baseline_commit)
    return ToolResult(ok=True, output={"reset_to": ctx.baseline_commit})
