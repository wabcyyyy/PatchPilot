"""工具回执 → 任务记忆观测(G06),以及模型的记忆提交通道 `update_memory`。

为什么"观测"由平台写而不是由模型写:模型在阶段末自述一段 findings,平台无从核对它到底读过
哪些行(SPEC 契约 5 要求"事实与模型假设分开")。这里把每条真实回执压成**一行可核对的事实**
(工具名 + 参数指向的对象 + 结果计数),由 registry 在工具执行处自动登记 —— 一次都不需要模型配合。

`update_memory` 是模型唯一的记忆写入面,它只能提交假设与排除方向,且必须点名依据;
状态词表里没有 `verified`(见 `TaskMemory.model_update`)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.context.task_memory import SOURCE_MODEL, STATUS_HYPOTHESIS
from app.tools.base import ToolResult

if TYPE_CHECKING:
    from app.tools.base import ToolContext

#: 这条工具自身的产物不进"观测"列表(它写的是假设,已有专门的区)
SELF_RECORDING_TOOLS = frozenset({"update_memory", "finish"})


def _first(items: object, limit: int = 3) -> str:
    if isinstance(items, list):
        return "、".join(str(x) for x in items[:limit]) + ("…" if len(items) > limit else "")
    return str(items or "")


def describe_receipt(
    name: str, args: dict[str, Any], output: Any, *, error: str | None = None
) -> tuple[str, str, str, str]:
    """把一次回执折成 `(detail, path, line_range, verdict)`;返回空 detail 表示不登记。

    只许写**回执里真实存在的数字**。不推测"这个文件很重要",也不把内容抄进记忆
    (内容在证据仓里,靠 `read_evidence` 引用 —— 记忆块要的是索引,不是第二份正文)。
    """
    if error is not None:
        # 失败同样是一条事实:它标出一个已经走死的口子,比成功更值得带给下一阶段
        return f"{name} 失败:{error[:120]}", str(args.get("path") or ""), "", ""
    if not isinstance(output, dict):
        return "", "", "", ""
    if name == "read_file":
        start = int(output.get("offset") or 1)
        count = int(output.get("returned_lines") or 0)
        end = start + max(0, count - 1)
        detail = (
            f"read_file {output.get('path', '')} 取 {count} 行"
            f"(全文 {output.get('total_lines', '?')} 行"
            + (",还有下文" if output.get("truncated") else ",已到文件尾")
            + ")"
        )
        return detail, str(output.get("path") or ""), f"{start}-{end}", ""
    if name == "search_code":
        hits = len(output.get("matches") or [])
        detail = (
            f"search_code {args.get('keyword', '')!r} 命中 {hits} 处"
            f"(引擎 {output.get('engine', '?')}"
            + (",结果被裁" if output.get("truncated") else "")
            + (",遍历域被裁(不是整仓)" if output.get("scope_truncated") else "")
            + ")"
        )
        files = []
        for match in output.get("matches") or []:
            if isinstance(match, dict) and match.get("path") and match["path"] not in files:
                files.append(str(match["path"]))
        return detail, _first(files, 3), "", ""
    if name == "find_symbol":
        hits = len(output.get("matches") or [])
        detail = (
            f"find_symbol {args.get('name', '')!r} match={output.get('match', '?')} "
            f"定义 {hits} 处" + ("(被上限裁过)" if output.get("truncated") else "")
        )
        paths = [
            str(m.get("path") or "") for m in (output.get("matches") or []) if isinstance(m, dict)
        ]
        return detail, _first([p for p in paths if p], 3), "", ""
    if name == "describe_file":
        if output.get("parse_error"):
            return (
                f"describe_file {output.get('path', '')} 解析失败,不可当大纲用",
                str(output.get("path") or ""),
                "",
                "",
            )
        return (
            f"describe_file {output.get('path', '')} 大纲 {output.get('count', 0)} 个符号"
            f"(全文 {output.get('total_lines', '?')} 行)",
            str(output.get("path") or ""),
            "",
            "",
        )
    if name == "list_files":
        return (
            f"list_files subdir={args.get('subdir') or '.'} 列出 {output.get('count', 0)} 个文件"
            + ("(被 500 条上限裁过,不是全仓)" if output.get("truncated") else ""),
            "",
            "",
            "",
        )
    if name == "git_diff":
        return (
            f"git_diff 改动 {len(output.get('changed_files') or [])} 个文件"
            f"(空补丁={output.get('is_empty')})：{_first(output.get('changed_files') or [])}",
            "",
            "",
            "",
        )
    if name == "apply_patch":
        return (
            f"apply_patch 应用成功,改动 {len(output.get('changed_files') or [])} 个文件:"
            f"{_first(output.get('changed_files') or [])}",
            "",
            "",
            "",
        )
    if name == "reset_workspace":
        return "reset_workspace 工作区已回到基线(此前的改动不再存在)", "", "", ""
    if name == "read_evidence":
        return (
            f"read_evidence {output.get('evidence_id', '')} 自偏移 {output.get('offset', 0)} 取回 "
            f"{output.get('returned_bytes', 0)}/{output.get('total_bytes', 0)} 字节",
            "",
            "",
            "",
        )
    if name == "run_tests":
        failed_ids = _first(
            [
                str(case.get("test_id") or case.get("name") or "")
                for case in (output.get("failed_cases") or [])
                if isinstance(case, dict)
            ],
            3,
        )
        verdict = (
            f"{output.get('test_set', '?')} 集 passed={output.get('passed', 0)} "
            f"failed={output.get('failed', 0)} errors={output.get('errors', 0)} "
            f"all_passed={output.get('all_passed')}"
            + (f";失败用例 {failed_ids}" if failed_ids else "")
            + (";超时" if output.get("timed_out") else "")
        )
        return f"run_tests {output.get('test_set', '?')}", "", "", verdict
    if name == "run_command":
        # 受控命令的回执(G09):只记事实读数,exit_code 不是验收
        stdout = output.get("stdout") or {}
        verdict = (
            f"profile={output.get('profile_id', '?')} exit_code={output.get('exit_code')} "
            + ("超时" if output.get("timed_out") else "")
            + ("被取消" if output.get("cancelled") else "")
            + f";stdout {stdout.get('raw_bytes', 0)}B"
            + (
                f"(另有 {stdout.get('dropped_bytes')}B 超额度未落盘)"
                if stdout.get("dropped_bytes")
                else ""
            )
            + f" evidence={stdout.get('evidence_id') or '未存'}"
            + (f";{output.get('environment')}" if output.get("environment") else "")
        )
        return f"run_command {output.get('profile_id', '?')}", "", "", verdict
    if name == "write_repro":
        return (
            f"write_repro {output.get('repro', '?')} "
            f"({output.get('bytes', 0)}B, sha256 {output.get('sha256', '?')},在 scratch 不进补丁)",
            "",
            "",
            "",
        )
    return "", "", "", ""


def update_memory(
    ctx: ToolContext,
    hypotheses: list[dict[str, Any]] | None = None,
    excluded: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """模型向任务记忆提交**假设/排除方向**的唯一通道(G06)。

    校验由 `TaskMemory.model_update` 承担:状态只许 `hypothesis|supported|refuted`,
    `supported/refuted` 必须带真实存在的证据引用,写 `verified/confirmed` 一律点名拒绝。
    返回值把"接受了几条、拒了哪条、为什么"原样给回模型 —— 被拒的原因本身就是反馈。
    """
    if ctx.memory is None:
        return ToolResult.fail("task memory is not available for this run")
    payload: dict[str, Any] = {"hypotheses": hypotheses or [], "excluded": excluded or []}
    applied, rejected = ctx.memory.model_update(payload)
    if ctx.session is not None:
        ctx.session.memory_version = ctx.memory.version
    return ToolResult(
        ok=True,
        output={
            "accepted": applied,
            "rejected": rejected,
            "memory_version": ctx.memory.version,
            "author": SOURCE_MODEL,
            "default_status": STATUS_HYPOTHESIS,
            "note": (
                "假设与排除已登记;平台判定(resolved/通过与否)不由这个工具产生,也不要在这里声明。"
            ),
        },
    )
