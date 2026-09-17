"""纯 Python 单循环 Agent(SWE-agent 风格)—— M3 教学实现。

M5 会把同一套工具与提示迁移到 LangGraph 状态机;
本实现保留作为"不用框架时的等价流程",也是理解状态机各节点的参照。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from app.config import get_settings
from app.errors import BudgetError, TaskCancelled
from app.gitops.differ import working_tree_diff
from app.llm.base import Model, messages_tokens
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext, ToolResult
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
    tokens_prompt: int = 0
    tokens_completion: int = 0


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
    allowed_tools: list[str] | None = None,
    token_budget: int | None = None,
    cancel_event: threading.Event | None = None,
) -> LoopOutcome:
    """工具循环:模型输出 → 解析工具调用 → 执行 → 结果回填 → 直到 finish。

    allowed_tools 限定本阶段可用的工具(如定位阶段禁用 apply_patch);None 不限制。
    token_budget 是本循环的 token 上限(None → 取 Settings.token_budget;0 不限制),
    按累计响应 token + 当前上下文 token 检查,超限抛 BudgetError。
    cancel_event 在每个 turn 开头(model.complete 之前)检查:已 set → 抛 TaskCancelled,
    即中断在下个 turn 边界生效,正在跑的一次 pytest/LLM 调用会先完成。
    """
    settings = get_settings()
    budget = settings.token_budget if token_budget is None else token_budget
    system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": issue_text},
    ]
    tokens_spent = 0
    tokens_prompt = 0
    tokens_completion = 0

    for turn_no in range(1, max_turns + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled(f"cancelled at turn {turn_no} boundary")
        context_tokens = messages_tokens(messages)
        if budget > 0 and tokens_spent + context_tokens > budget:
            raise BudgetError(
                f"agent loop tokens {tokens_spent + context_tokens} exceed budget {budget}"
            )
        response = model.complete(messages, tool_schemas())  # type: ignore[arg-type]
        tokens_spent += response.usage_tokens
        tokens_prompt += response.prompt_tokens
        tokens_completion += response.completion_tokens

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
                    tokens_used=tokens_spent,
                    patch_applied=_has_patch(ctx),
                    finish_declared=True,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
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

            if allowed_tools is not None and call.name not in allowed_tools:
                result = ToolResult.fail(
                    f"tool {call.name!r} is not allowed in phase {state_label}"
                )
                ctx.tracker.record(
                    tool=call.name,
                    round_no=round_no,
                    state=state_label,
                    input_payload={"blocked": True},
                    error=result.error,
                )
            else:
                result = execute(
                    ctx, call.name, call.arguments, round_no=round_no, state=state_label
                )
            payload = result.output if result.ok else {"error": result.error}
            content = json.dumps(payload, ensure_ascii=False, default=str)
            if len(content) > 8000:
                content = content[:8000] + "... (truncated)"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise BudgetError(f"agent loop exceeded max_turns={max_turns}")
