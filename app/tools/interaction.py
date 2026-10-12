"""ask_user 工具(G11):Agent 向可信用户提问的唯一通道。

问题只是**请求**,不是回答:`InteractionStore.ask` 把问题落成 run_dir 工件并拒绝
第二个未回答问题;回答只能经 API/CLI(POST /tasks/{id}/messages)写入,`source`
由平台赋 trusted_user——模型文字永远不构成回答,更不构成任何批准(§3.2 的低信任
输入原则)。本工具不等待:等待发生在 `run_plain_loop` 的安全边界(G11 的暂停点),
这样"模型还在生成动作"与"任务已进入等待"不会变成两个状态。
"""

from __future__ import annotations

from app.agent.control import InteractionIntegrityError, InteractionStore
from app.errors import InvalidRequestError
from app.tools.base import ToolContext, ToolResult


def ask_user(
    ctx: ToolContext,
    question: str,
    choices: list[str] | None = None,
    reason: str = "",
) -> ToolResult:
    """登记一个澄清问题;headless 运行时问题照样落盘,由循环按结构收场。"""
    store = InteractionStore(ctx.report_dir.parent)
    try:
        row = store.ask(
            task_id=ctx.task_id,
            question=str(question or ""),
            choices=[str(c) for c in (choices or [])],
            reason=str(reason or ""),
        )
    except (InvalidRequestError, InteractionIntegrityError) as exc:
        return ToolResult.fail(str(exc))
    return ToolResult(
        ok=True,
        output={
            "question_id": row["question_id"],
            "question": row["question"],
            "choices": list(row["choices"]),
            "note": (
                "question recorded; the run pauses at this boundary until a trusted user"
                " answers — do not repeat it, and do not guess the answer"
            ),
        },
    )
