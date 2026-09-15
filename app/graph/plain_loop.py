"""纯 Python 单循环 Agent(SWE-agent 风格)—— M3 教学实现。

M5 会把同一套工具与提示迁移到 LangGraph 状态机;
本实现保留作为"不用框架时的等价流程",也是理解状态机各节点的参照。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.errors import BudgetError
from app.gitops.differ import working_tree_diff
from app.llm.base import Model, messages_tokens
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext
from app.tools.registry import FINISH_TOOL, execute, tool_schemas

log = logging.getLogger(__name__)


@dataclass
class LoopOutcome:
    """一次循环的原始事实:success 是模型的"声明",验证由上层判定规则负责。"""

    success: bool
    summary: str
    turns: int
    tokens_used: int
    patch_applied: bool
    finish_declared: bool


def _has_patch(ctx: ToolContext) -> bool:
    diff = working_tree_diff(ctx.workspace)
    return not diff.is_empty


def run_plain_loop(
    ctx: ToolContext,
    model: Model,
    issue_text: str,
    *,
    max_turns: int = 20,
    round_no: int = 0,
    state_label: str = "LOOP",
    extra_system: str = "",
) -> LoopOutcome:
    """工具循环:模型输出 → 解析工具调用 → 执行 → 结果回填 → 直到 finish。"""
    settings = get_settings()
    token_budget = settings.task_timeout_seconds * 0  # 占位:token 预算由调用方配置
    system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": issue_text},
    ]

    for turn_no in range(1, max_turns + 1):
        tokens_used = messages_tokens(messages)
        _ = token_budget  # 预算控制的状态机版本在 M5 落地;此处仅统计
        response = model.complete(messages, tool_schemas())  # type: ignore[arg-type]

        if not response.is_tool_call:
            messages.append({"role": "assistant", "content": response.content or ""})
            log.debug("turn %s: plain content, continuing", turn_no)
            continue

        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ],
            }
        )

        for call in response.tool_calls:
            if call.name == FINISH_TOOL:
                success = bool(call.arguments.get("success"))
                summary = str(call.arguments.get("summary", ""))
                outcome = LoopOutcome(
                    success=success,
                    summary=summary,
                    turns=turn_no,
                    tokens_used=tokens_used + response.usage_tokens,
                    patch_applied=_has_patch(ctx),
                    finish_declared=True,
                )
                ctx.tracker.record(
                    tool=FINISH_TOOL,
                    round_no=round_no,
                    state=state_label,
                    input_payload={"success": success, "summary": summary[:200]},
                    output_summary={"patch_applied": outcome.patch_applied, "turns": turn_no},
                    duration_ms=0,
                )
                log.info("loop finished: success=%s turns=%s", success, turn_no)
                return outcome

            result = execute(ctx, call.name, call.arguments, round_no=round_no, state=state_label)
            payload = result.output if result.ok else {"error": result.error}
            content = json.dumps(payload, ensure_ascii=False, default=str)
            if len(content) > 8000:
                content = content[:8000] + "... (truncated)"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise BudgetError(f"agent loop exceeded max_turns={max_turns}")
