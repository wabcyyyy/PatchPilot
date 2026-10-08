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
from app.context.token_window import compact_messages
from app.errors import BudgetError, TaskCancelled
from app.gitops.differ import working_tree_diff
from app.graph.loop_state import LoopSnapshot, delete_loop_snapshot, save_loop_snapshot
from app.graph.resources import USAGE_ESTIMATED, ResourceLedger
from app.llm.base import AssistantTurn, Model, messages_tokens
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext, ToolResult
from app.tools.output_filter import fold_output
from app.tools.registry import FINISH_TOOL, _summarize_input, execute, tool_schemas

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


def _compact_working_memory(
    ctx: ToolContext,
    messages: list[dict[str, object]],
    *,
    threshold: int,
    keep_recent_turns: int,
    round_no: int,
    state_label: str,
    turn_no: int,
) -> tuple[list[dict[str, object]], int]:
    """工作记忆超阈值时压缩一档,返回 (新消息列表, 压缩后 token 估算)。

    只缩小"发给模型的内容",不动预算口径:压缩后的 context_tokens 仍要过下面那条
    一字未改的 BudgetError 门禁(压缩救不回来就该终止还是得终止)。存根保留工具名与
    关键入参,模型才知道"这条已经查过"——实测死因正是只读调查原地重复烧额度
    (runs/swe-hard-graph*:16-19 轮、417,894 tokens、apply_patch 0 次)。
    """
    result = compact_messages(
        messages,
        max_context_tokens=threshold,
        keep_recent_turns=keep_recent_turns,
    )
    if not result.already_within and (result.stubbed or result.dropped):
        ctx.tracker.record(
            tool="context_compact",
            round_no=round_no,
            state=state_label,
            input_payload={"before_tokens": result.before_tokens, "threshold": threshold},
            output_summary={
                "after_tokens": result.after_tokens,
                "stubbed": result.stubbed,
                "dropped": result.dropped,
                "state": state_label,
                "turn": turn_no,
            },
        )
    return result.messages, result.after_tokens


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
    context_window_tokens: int | None = None,
    context_keep_recent_turns: int | None = None,
    token_estimate_factor: float | None = None,
    resume_snapshot: LoopSnapshot | None = None,
    ledger: ResourceLedger | None = None,
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
    context_window_tokens 是工作记忆的**软阈值**(None = 跟随 Settings.context_window_tokens;
    显式传 0 = 关闭,行为与此前逐字一致):超过它
    就在 turn 边界压缩历史(见 app/context/token_window.py),然后再走既有预算门禁。
    压缩只让"发出去的内容"变小,不放宽任何额度——压完仍超预算照样 BudgetError。
    resume_snapshot(M6 A 级恢复):崩溃前某个 turn 边界的 LoopSnapshot。阶段与轮次
    匹配时用它作种子——messages、三个 token 计数、last_content 全部续接,循环从
    `turn_no + 1` 起跑。**轮次上限不重授**:仍按同一个 max_turns 判定,恢复只是
    接着跑完剩下的轮次,不是再给一次完整额度(否则一个任务能靠反复崩溃刷出 N 倍轮次)。
    不匹配(或为 None)即冷启动,与既有行为逐字一致。
    快照写入受 Settings.loop_snapshot_enabled 控制,落在 run_dir/loop_state.json,
    每个 turn 边界写一次(工具结果全部回填后才是一致切点);阶段正常 finish 时删除。
    """
    settings = get_settings()
    snapshot_enabled = bool(getattr(settings, "loop_snapshot_enabled", False))
    budget = settings.token_budget if token_budget is None else token_budget
    # None = 跟随 Settings(与 token_budget 同一约定)。这样"压缩口径"不会因为某个调用方
    # 忘了传参就悄悄不一致——两臂对照尤其吃这条:少一边压缩就是多一个未登记的消融变量。
    window = (
        settings.context_window_tokens if context_window_tokens is None else context_window_tokens
    )
    keep_recent = (
        settings.context_keep_recent_turns
        if context_keep_recent_turns is None
        else context_keep_recent_turns
    )
    est_factor = (
        settings.token_estimate_factor if token_estimate_factor is None else token_estimate_factor
    )
    system = SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": issue_text},
    ]
    tokens_spent = 0
    tokens_prompt = 0
    tokens_completion = 0
    last_content = ""  # 最近一次有实质文本的模型输出,供上层在额度耗尽时降级取用
    start_turn = 1

    if resume_snapshot is not None:
        if (
            resume_snapshot.stage == state_label
            and resume_snapshot.round_no == round_no
            and resume_snapshot.messages
        ):
            # 续接的是**累计量**:恢复后 token 继续累加、轮次继续计数,不重置也不重授
            messages = [dict(m) for m in resume_snapshot.messages]
            tokens_spent = resume_snapshot.tokens_spent
            tokens_prompt = resume_snapshot.tokens_prompt
            tokens_completion = resume_snapshot.tokens_completion
            last_content = resume_snapshot.last_content
            start_turn = resume_snapshot.turn_no + 1
            ctx.tracker.record(
                tool="loop_resume",
                round_no=round_no,
                state=state_label,
                input_payload={"from_turn": start_turn, "snapshot_turn": resume_snapshot.turn_no},
                output_summary={
                    "messages": len(messages),
                    "tokens_spent": tokens_spent,
                    "max_turns": max_turns,
                },
            )
        else:
            # 阶段/轮次不符或消息为空:宁可冷启动重跑,也不把别处的历史灌进本阶段
            log.warning(
                "task %s: resume snapshot mismatched (stage=%s round=%s), cold start",
                ctx.task_id,
                resume_snapshot.stage,
                resume_snapshot.round_no,
            )

    def _write_snapshot(turn_no: int) -> None:
        """落一份当前 turn 边界的工作记忆;失败只记日志(见 loop_state 模块边界)。"""
        if not snapshot_enabled:
            return
        save_loop_snapshot(
            ctx,
            LoopSnapshot(
                stage=state_label,
                round_no=round_no,
                turn_no=turn_no,
                messages=messages,
                tokens_spent=tokens_spent,
                tokens_prompt=tokens_prompt,
                tokens_completion=tokens_completion,
                last_content=last_content,
                task_id=ctx.task_id,
            ),
        )

    for turn_no in range(start_turn, max_turns + 1):
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
        if window > 0 and context_tokens > window:
            # 压缩必须排在预算检查**之前**:先给工作记忆一个公平的机会变小,
            # 再用同一条门禁判定(见下),否则"能压下来也照旧死"
            messages, context_tokens = _compact_working_memory(
                ctx,
                messages,
                threshold=window,
                keep_recent_turns=keep_recent,
                round_no=round_no,
                state_label=state_label,
                turn_no=turn_no,
            )
        # 混单位门禁:`tokens_spent` 是 provider 真值,`context_tokens` 是本地估算。
        # est_factor 只换算**待发的那一次**,默认 1.0 时读数与本字段引入前逐字相同(M15 的
        # V1 锚点就吃这个读数,不许回溯改口径)。
        pending = round(context_tokens * est_factor)
        if budget > 0 and tokens_spent + pending > budget:
            raise _budget_error(
                f"agent loop tokens {tokens_spent + pending} exceed budget {budget}",
                tokens_spent,
                tokens_prompt,
                tokens_completion,
                turns=turn_no,
                last_content=last_content,
            )
        # S02/F1:任务级账本——请求前记在途调用并做总额检查(与阶段份额分开);
        # 总额不够发这次请求 = 停止新模型调用的信号(exhausted),不是阶段降级
        call_id = ""
        if ledger is not None:
            try:
                ledger.ensure_request_fits(pending)
            except BudgetError as exc:
                raise _budget_error(
                    str(exc),
                    tokens_spent,
                    tokens_prompt,
                    tokens_completion,
                    turns=turn_no,
                    last_content=last_content,
                ) from exc
            call_id = ledger.begin_call(stage=state_label, round_no=round_no)
        llm_started = time.monotonic()
        try:
            response = model.complete(messages, tool_schemas())  # type: ignore[arg-type]
        except BudgetError as exc:
            # 预算异常携带的是**可知消耗**(N-11):按 estimated 入账保留,
            # 不标 unknown——桩/真实端点用它表达"这次请求花了多少"是合法控制流
            if ledger is not None and call_id:
                ledger.record_usage(
                    call_id,
                    prompt_tokens=exc.tokens_prompt,
                    completion_tokens=exc.tokens_completion,
                    total_tokens=exc.tokens_spent,
                    source=USAGE_ESTIMATED,
                )
            raise
        except BaseException as exc:
            # 在途调用拿不到真实 usage:标 unknown(不写 0、不重授余量),异常原样上抛
            if ledger is not None and call_id:
                ledger.abandon_call(call_id, reason=f"{type(exc).__name__}")
            raise
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
        # S02/F1:回复的真实 usage **先入账**——若这一条把任务推过限额,本回复的
        # 任何工具调用(含 finish)都不再执行,结构化收尾;已花的数字如实保留
        if ledger is not None and call_id:
            overrun = ledger.record_usage(
                call_id,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            if overrun is not None:
                ctx.tracker.record(
                    tool="resource_overrun",
                    round_no=round_no,
                    state=state_label,
                    input_payload={"call_id": call_id, "turn": turn_no},
                    output_summary={
                        "tokens_used": ledger.tokens_used,
                        "token_limit": ledger.token_limit,
                        "usage_source": "provider",
                    },
                    error=str(overrun),
                )
                raise _budget_error(
                    f"task token budget exceeded after reply: {overrun}",
                    tokens_spent,
                    tokens_prompt,
                    tokens_completion,
                    turns=turn_no,
                    last_content=response.content or last_content,
                )

        if not response.is_tool_call:
            messages.append(_assistant_payload(response))
            log.debug("turn %s: plain content, continuing", turn_no)
            _write_snapshot(turn_no)
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
                    # summary 就是下一阶段的 findings,所以截断走工具入参同一条规则(>300 留
                    # "... (N chars)"):调用点各自截断会把上一阶段结论的尺寸从证据链里抹掉(M16.9)。
                    input_payload=_summarize_input(
                        FINISH_TOOL, {"success": success, "summary": summary}
                    ),
                    output_summary={"patch_applied": outcome.patch_applied, "turns": turn_no},
                    duration_ms=0,
                )
                log.info(
                    "task %s loop finished: success=%s turns=%s",
                    ctx.task_id,
                    success,
                    turn_no,
                )
                # M6:阶段正常收尾 → 作废快照。已完成过的阶段再被续跑会把同一阶段
                # 重放第二遍(轮次/token 双计),所以"finish 即删"是恢复路径的前提,
                # 与 loop_snapshot_enabled 开关无关(关掉开关也可能留有上一次开的残档)
                delete_loop_snapshot(ctx, task_id=ctx.task_id)
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

        # turn 边界:本轮所有工具结果都已回填,这是唯一可安全续跑的切点
        _write_snapshot(turn_no)

    raise _budget_error(
        f"agent loop exceeded max_turns={max_turns}",
        tokens_spent,
        tokens_prompt,
        tokens_completion,
        turns=max_turns,
        last_content=last_content,
    )
