"""纯 Python 单循环 Agent(SWE-agent 风格)—— M3 教学实现。

M5 会把同一套工具与提示迁移到 LangGraph 状态机;
本实现保留作为"不用框架时的等价流程",也是理解状态机各节点的参照。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from app.config import get_settings
from app.errors import BudgetError, TaskCancelled
from app.gitops.differ import working_tree_diff
from app.llm.base import AssistantTurn, Model, messages_tokens
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext, ToolResult
from app.tools.output_filter import fold_output
from app.tools.registry import FINISH_TOOL, execute, tool_schemas

log = logging.getLogger(__name__)

_MAX_THOUGHT_CHARS = 2000


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


def _budget_error(
    message: str,
    tokens_spent: int,
    tokens_prompt: int,
    tokens_completion: int,
    turns: int = 0,
    last_content: str = "",
) -> BudgetError:
    """构造携带已耗用量的 BudgetError(N-11 整改)。

    此前循环抛出预算异常时,本循环已烧掉的 token/turns 只存在于局部变量里,
    上层把异常翻译成终态后这些用量"蒸发"——任务级 report.json 的 tokens_used
    系统性低估,且任务级预算余量核算失真。

    last_content 是模型最后一轮的实质文本:调用方据此判断"定位段额度用完了但任务级
    还有余量"时可以拿它当暂定结论继续,而不是把整个任务判死。
    """
    return BudgetError(
        message,
        tokens_spent=tokens_spent,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        turns=turns,
        last_content=last_content,
    )


def _record_thought(
    ctx: ToolContext,
    response: AssistantTurn,
    *,
    state_label: str,
    round_no: int,
    turn_no: int,
    duration_ms: int,
) -> None:
    """把模型本轮输出(Thought 侧)记入轨迹,补全 Thought→Action→Observation。

    content 截断到 _MAX_THOUGHT_CHARS;tool_calls 只记工具名,参数与结果
    由工具执行事件(registry.execute)记录,不重复。
    """
    content = response.content or ""
    if len(content) > _MAX_THOUGHT_CHARS:
        content = content[:_MAX_THOUGHT_CHARS] + f"... ({len(content)} chars)"
    ctx.tracker.record(
        tool="llm",
        round_no=round_no,
        state=state_label,
        input_payload={"turn": turn_no},
        output_summary={
            "content": content,
            "tool_calls": [c.name for c in response.tool_calls],
            "tokens": response.usage_tokens,
        },
        duration_ms=duration_ms,
    )


def _assistant_payload(response: AssistantTurn) -> dict[str, object]:
    """回填给下一轮的 assistant 消息体。

    reasoning_content 必须原样带回:思考模式端点(deepseek 等)校验"thinking 模式下
    assistant 消息缺思维链就 400",实测让整题崩成 NEEDS_REVIEW(花了钱还不入账)。
    非思考端点该字段恒 None,消息形态与此前完全一致。
    """
    payload: dict[str, object] = {"role": "assistant", "content": response.content or ""}
    if response.reasoning_content:
        payload["reasoning_content"] = response.reasoning_content
    return payload


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
    started_monotonic: float | None = None,
    time_budget_seconds: int = 0,
    cancel_event: threading.Event | None = None,
) -> LoopOutcome:
    """工具循环:模型输出 → 解析工具调用 → 执行 → 结果回填 → 直到 finish。

    allowed_tools 限定本阶段可用的工具(如定位阶段禁用 apply_patch);None 不限制。
    token_budget 是本循环的 token 上限(None → 取 Settings.token_budget;0 不限制),
    按累计响应 token + 当前上下文 token 检查,超限抛 BudgetError。
    started_monotonic/time_budget_seconds 是任务级时间预算(N-10 整改):此前只有
    "进入循环前查一次",循环内一次 LLM 调用 + 一次 pytest 可远超剩余额度,
    锁 TTL 会早于任务结束——现在每个 turn 边界都复查。
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
    last_content = ""  # 最近一次有实质文本的模型输出,供上层在额度耗尽时降级取用

    for turn_no in range(1, max_turns + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled(f"cancelled at turn {turn_no} boundary")
        if (
            started_monotonic is not None
            and time_budget_seconds > 0
            and time.monotonic() - started_monotonic > time_budget_seconds
        ):
            raise _budget_error(
                f"agent loop exceeded time budget {time_budget_seconds}s at turn {turn_no}",
                tokens_spent,
                tokens_prompt,
                tokens_completion,
                turns=turn_no,
                last_content=last_content,
            )
        context_tokens = messages_tokens(messages)
        if budget > 0 and tokens_spent + context_tokens > budget:
            raise _budget_error(
                f"agent loop tokens {tokens_spent + context_tokens} exceed budget {budget}",
                tokens_spent,
                tokens_prompt,
                tokens_completion,
                turns=turn_no,
                last_content=last_content,
            )
        llm_started = time.monotonic()
        response = model.complete(messages, tool_schemas())  # type: ignore[arg-type]
        llm_ms = int((time.monotonic() - llm_started) * 1000)
        tokens_spent += response.usage_tokens
        tokens_prompt += response.prompt_tokens
        tokens_completion += response.completion_tokens
        if response.content:
            last_content = response.content
        _record_thought(
            ctx,
            response,
            state_label=state_label,
            round_no=round_no,
            turn_no=turn_no,
            duration_ms=llm_ms,
        )

        if not response.is_tool_call:
            messages.append(_assistant_payload(response))
            log.debug("turn %s: plain content, continuing", turn_no)
            continue

        assistant_payload = _assistant_payload(response)
        assistant_payload["tool_calls"] = [
            {
                "type": "function",
                "id": c.id,
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in response.tool_calls
        ]
        messages.append(assistant_payload)

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
                log.info(
                    "task %s loop finished: success=%s turns=%s",
                    ctx.task_id,
                    success,
                    turn_no,
                )
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
            # 模型可见层折叠:头 tail 保留 + hard_cap 兜底(原始报告/junit 证据不动)
            content = fold_output(
                content,
                head=settings.refine_head_lines,
                tail=settings.refine_tail_lines,
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise _budget_error(
        f"agent loop exceeded max_turns={max_turns}",
        tokens_spent,
        tokens_prompt,
        tokens_completion,
        turns=max_turns,
        last_content=last_content,
    )
