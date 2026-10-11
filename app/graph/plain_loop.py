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
from pathlib import Path

from app.agent.session import SessionState
from app.config import get_settings
from app.context.task_memory import save_memory_state
from app.context.token_window import compact_messages
from app.errors import BudgetError, TaskCancelled
from app.gitops.differ import working_tree_diff
from app.graph.loop_state import LoopSnapshot, delete_loop_snapshot, save_loop_snapshot
from app.graph.resources import USAGE_ESTIMATED, ResourceLedger
from app.llm.base import AssistantTurn, Model, messages_tokens
from app.llm.usage import ModelResponseEnvelope, UsageEnvelope, estimate_request
from app.prompts import SYSTEM_PROMPT, TEXT_COMPLETION_REPAIR
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import FINISH_TOOL, _summarize_input, execute, model_payload, tool_schemas

log = logging.getLogger(__name__)

_MAX_THOUGHT_CHARS = 2000
# 文本完成契约(G01/R1)允许的次数修正额度:1 次,不多给。
# 依据是实测而非口味:真实 Q0 的 PLAN 段发了 132 次请求、20/21 题以 phase_share 耗尽降级收场,
# "再问一次"在那批数据里就是"把同一个不合规回复再烧一份份额",额度放大只会放大缺陷。
MAX_PROTOCOL_REPAIRS = 1


@dataclass
class LoopOutcome:
    """一次循环的原始事实:success 是模型的"声明",验证由上层判定规则负责。

    completion 记的是**怎么结束的**,不是结束得好不好:
    - "finish":走了 finish 伪工具(引入文本完成契约之前的唯一正常出口);
    - "text":文本完成契约下的正常收尾——非空、未被 max_tokens 截断、没要求任何工具;
    - "protocol_invalid":文本完成契约下用完全额修正仍不合协议(空回复/截断/要工具),
      上层按它落降级事件,不能再拿 max_turns/phase_share 当它的出口。
    success 在 "text" 一路是"阶段协议达成"的意思(计划段没有任何写工具,更没有补丁),
    平台的 resolved 判定与此无关,任何调用方都不许从这里读修复结论。
    """

    success: bool
    summary: str
    turns: int
    tokens_used: int
    patch_applied: bool
    finish_declared: bool
    tokens_prompt: int = 0
    tokens_completion: int = 0
    completion: str = "finish"
    protocol_reason: str = ""


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


def _usage_of(response: AssistantTurn) -> UsageEnvelope:
    """这次回复的计量事实:模型层给了 envelope 就用它,旧桩/回放按 estimated 如实换算。

    `AssistantTurn` 上那三个 int 继续是各处读的**读模型**;"这笔数是端点真值、本地估算
    还是无从知道"只由 envelope 回答(G02 之前由 `record_usage` 的默认值 provider 代答,
    于是回放与估算全被标成真值)。
    """
    if response.envelope is not None:
        return response.envelope.usage
    return UsageEnvelope.from_turn_ints(
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        total_tokens=response.usage_tokens,
    )


def _record_thought(
    ctx: ToolContext,
    response: AssistantTurn,
    *,
    state_label: str,
    round_no: int,
    turn_no: int,
    duration_ms: int,
    request_kind: str,
) -> None:
    """把模型本轮输出(Thought 侧)记入轨迹,补全 Thought→Action→Observation。

    content 截断到 _MAX_THOUGHT_CHARS;tool_calls 只记工具名,参数与结果
    由工具执行事件(registry.execute)记录,不重复。
    reasoning_content **不进轨迹**:轨迹经 API 原样对外(`GET /tasks/{id}/trajectory`),
    思维链原文属公开面(T24),这里只记"有没有返回"和它的 token 明细。
    """
    content = response.content or ""
    if len(content) > _MAX_THOUGHT_CHARS:
        content = content[:_MAX_THOUGHT_CHARS] + f"... ({len(content)} chars)"
    usage = _usage_of(response)
    envelope = response.envelope
    summary: dict[str, object] = {
        "content": content,
        "tool_calls": [c.name for c in response.tool_calls],
        "tokens": response.usage_tokens,
        # 计量归类(G02):来源、是否被 max_tokens 截断、这一轮请求是哪一类请求
        "usage_source": usage.usage_source,
        "finish_reason": response.finish_reason,
        "request_kind": request_kind,
    }
    if usage.prompt_tokens is not None:
        summary["prompt_tokens"] = usage.prompt_tokens
    if usage.completion_tokens is not None:
        summary["completion_tokens"] = usage.completion_tokens
    if usage.cached_tokens is not None:
        summary["cached_tokens"] = usage.cached_tokens
    if usage.anomalies:
        summary["usage_anomalies"] = list(usage.anomalies)
    if envelope is not None:
        summary["physical_attempts"] = envelope.physical_attempts
        # 排序后落 list:轨迹是 JSONL,set 会让 json.dumps 当场抛
        summary["retry_owner"] = sorted({a.retry_owner for a in envelope.attempts})
        if envelope.parse_failures:
            summary["parse_failures"] = list(envelope.parse_failures)
        if envelope.request_estimate is not None:
            summary["request_estimate_tokens"] = envelope.request_estimate.total_tokens
            summary["estimate_version"] = envelope.request_estimate.version
    ctx.tracker.record(
        tool="llm",
        round_no=round_no,
        state=state_label,
        input_payload={"turn": turn_no},
        output_summary=summary,
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


def _protocol_violation(response: AssistantTurn) -> str:
    """文本完成契约下这一轮的判定:合规 → 空串;否则给出不合规的原因。

    "legacy_finish" 是**不消耗修正额度**的一种:旧回放与部分端点会把"文本 + finish"
    当收尾,SPEC 要求这条路继续等效可用,所以它交回既有的 finish 分支处理。
    被 max_tokens 截断的回复即便带着 finish 也不算收尾——截断处的 arguments 是废的
    (与 openai_client 对 length + tool_calls 早失败的口径同源)。
    """
    truncated = response.finish_reason == "length"
    if response.is_tool_call:
        if truncated:
            return "truncated"
        if {call.name for call in response.tool_calls} <= {FINISH_TOOL}:
            return "legacy_finish"
        return "illegal_tool"
    if truncated:
        return "truncated"
    if not (response.content or "").strip():
        return "empty_reply"
    return ""


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
    complete_on_text: bool = False,
    token_budget: int | None = None,
    deadline_epoch: float | None = None,
    cancel_event: threading.Event | None = None,
    context_window_tokens: int | None = None,
    context_keep_recent_turns: int | None = None,
    token_estimate_factor: float | None = None,
    resume_snapshot: LoopSnapshot | None = None,
    ledger: ResourceLedger | None = None,
) -> LoopOutcome:
    """工具循环:模型输出 → 解析工具调用 → 执行 → 结果回填 → 直到 finish。

    allowed_tools 限定本阶段可用的工具(如定位阶段禁用 apply_patch);None 不限制。
    它同时决定**模型可见的 schema 集合**(G01/R2):此前可见面恒为全量 10 个、白名单只在
    执行侧兜底,模型每越权一次就白烧一轮请求(先调用一个本阶段根本没有的手段,拿到一条
    "not allowed in phase" 再重想)。
    complete_on_text 打开**文本完成契约**(计划段用,G01/R1):一段非空、未被 max_tokens
    截断、且不要求任何工具的回复即视为本阶段正常结束,不再依赖 finish 伪工具才出口;
    空回复/截断/越权工具调用最多换一次协议修正,仍不合规就以 completion="protocol_invalid"
    结构化结束。默认 False = 既有行为逐字不变(纯文本继续下一轮,只有 finish 返回)。
    token_budget 是本循环的 token 上限(None → 取 Settings.token_budget;0 不限制),
    按累计响应 token + 当前上下文 token 检查,超限抛 BudgetError。
    deadline_epoch 是任务级时间预算(S02b,ADR-0009 §3):持久化的墙钟截止时刻,
    执行启动时建立并进 checkpoint,恢复沿用原值的剩余时间(不重授);每个
    turn 边界都复查——一次 LLM 调用 + 一次 pytest 可远超剩余额度。
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
    # 可见 schema 在**任何请求发出之前**按本阶段能力集合装配一次:白名单里写了注册表没有的
    # 名字会在这里抛错,而不是等到模型看不见某个工具、或调了才吃 "unknown tool"。
    schemas = tool_schemas(allowed_tools)
    tokens_spent = 0
    tokens_prompt = 0
    tokens_completion = 0
    repairs_used = 0  # 文本完成契约的协议修正次数(上限 MAX_PROTOCOL_REPAIRS)
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
        """落一份当前 turn 边界的工作记忆;失败只记日志(见 loop_state 模块边界)。

        G06 把**会话身份与记忆版本一起绑进快照**:恢复方比的就是"这条会话、这一版记忆"。
        记忆与快照必须在同一个边界落盘 —— 分两处写就会出现"快照是第 19 轮、记忆是第 12 轮"
        的错配,而 PROPOSE 看到的证据索引已经不是崩溃前那一刻的那份。
        """
        if not snapshot_enabled:
            return
        session = getattr(ctx, "session", None)
        memory = getattr(ctx, "memory", None)
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
                session_id=session.session_id if session is not None else "",
                session_hash=session.identity_hash() if session is not None else "",
                memory_version=memory.version if memory is not None else 0,
                memory_hash=memory.state_hash() if memory is not None else "",
            ),
        )
        if memory is not None:
            save_memory_state(Path(ctx.report_dir).parent, memory)

    for turn_no in range(start_turn, max_turns + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled(f"cancelled at turn {turn_no} boundary")
        if deadline_epoch is not None and time.time() > deadline_epoch:
            raise _budget_error(
                f"task exceeded time budget at turn {turn_no} (deadline passed)",
                tokens_spent,
                tokens_prompt,
                tokens_completion,
                turns=turn_no,
                last_content=last_content,
            )
        # 配对不变量(G06):即将发出去的消息里若有"assistant 声明了 tool_call 但没有回执",
        # 严格端点直接 400(实测整题崩成 NEEDS_REVIEW)。这条检查放在**唯一**的发送前点,
        # 而不是指望四个回填分支各自记得配对;补上的回执是显式标注的占位串,不伪造成功。
        messages, repaired = SessionState.repair_orphans(messages)
        if repaired:
            ctx.tracker.record(
                tool="message_pairing_repaired",
                round_no=round_no,
                state=state_label,
                input_payload={"turn": turn_no},
                output_summary={"placeholder_replies": repaired, "messages": len(messages)},
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
                call_id = ledger.begin_call(stage=state_label, round_no=round_no)
                # 原子预留(G03):检查与登记在同一临界区完成,比较基数是
                # "已用 + 全部在途预留 + 本次预留"。此前只看已用,两个并发请求
                # 各自都装得下就都发出去了,现金/额度上限形同一半。
                # 预留量用**待发请求的完整容量估算**(含有效 schema、tool arguments、
                # 需回传的 reasoning 与协议开销),而不是上面那个被历史锚点钉住的闸门读数。
                ledger.reserve(
                    call_id=call_id,
                    stage=state_label,
                    round_no=round_no,
                    request_tokens=estimate_request(messages, schemas).total_tokens,
                )
            except BudgetError as exc:
                raise _budget_error(
                    str(exc),
                    tokens_spent,
                    tokens_prompt,
                    tokens_completion,
                    turns=turn_no,
                    last_content=last_content,
                ) from exc
        llm_started = time.monotonic()
        request_kind = "protocol_repair" if repairs_used else "normal"
        try:
            response = model.complete(messages, schemas)  # type: ignore[arg-type]
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
            # 异常先计量(G02 / 验收矩阵 T03):端点**已经回复**但结构解释不了时,
            # 已知消耗先入账再让异常按原路径收尾;真正什么都不知道才标 unknown。
            # 此前这里一律 abandon_call,"花了 321 token 但 choices 为空"和"连接都没建立"
            # 在账本上长成同一个数,而实际前者已经产生费用、后者没有。
            carried = getattr(exc, "envelope", None)
            if ledger is not None and call_id:
                usage = getattr(carried, "usage", None)
                if isinstance(usage, UsageEnvelope) and usage.has_known_usage:
                    ledger.record_usage(
                        call_id,
                        prompt_tokens=usage.prompt_tokens or 0,
                        completion_tokens=usage.completion_tokens or 0,
                        total_tokens=usage.chargeable_total,
                        source=usage.usage_source,
                        usage=usage,
                    )
                else:
                    # 在途调用拿不到真实 usage:标 unknown(不写 0、不重授余量),异常原样上抛
                    ledger.abandon_call(call_id, reason=f"{type(exc).__name__}")
            if isinstance(carried, ModelResponseEnvelope) and carried.parse_failures:
                # 解析失败本身进轨迹:报告里要能看出"这笔花费买到的是一个没解释成功的回复"
                ctx.tracker.record(
                    tool="model_response_invalid",
                    round_no=round_no,
                    state=state_label,
                    input_payload={"call_id": call_id, "turn": turn_no},
                    output_summary={
                        "parse_failures": list(carried.parse_failures),
                        "finish_reason": carried.finish_reason,
                        "truncated": carried.truncated,
                        "physical_attempts": carried.physical_attempts,
                        "usage_source": carried.usage.usage_source,
                        "known_tokens": carried.usage.chargeable_total,
                        "unknown_attempts": carried.usage.unknown_attempts,
                    },
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        llm_ms = int((time.monotonic() - llm_started) * 1000)
        usage = _usage_of(response)
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
            request_kind=request_kind,
        )
        # S02/F1:回复的真实 usage **先入账**——若这一条把任务推过限额,本回复的
        # 任何工具调用(含 finish)都不再执行,结构化收尾;已花的数字如实保留
        if ledger is not None and call_id:
            overrun = ledger.record_usage(
                call_id,
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                total_tokens=usage.chargeable_total,
                source=usage.usage_source,
                usage=usage,
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
                        "usage_source": usage.usage_source,
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

        if cancel_event is not None and cancel_event.is_set():
            # 取消与回复同时到达:这一次花费**已经在上面结算过了**,但本回复的工具调用
            # 一个都不执行(G03 的"晚到回复只计量不行动")。快照仍要落盘 —— 取消点是
            # 可续位置(test_resume 钉着这条),所以把"被取消挡下"的 tool 回执补齐,
            # 不留下没有配对消息的孤儿 tool_call。
            assistant_payload = _assistant_payload(response)
            assistant_payload["tool_calls"] = [
                {
                    "type": "function",
                    "id": c.id,
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in response.tool_calls
            ]
            messages.append(assistant_payload)
            for call in response.tool_calls:
                error = "task cancelled before this tool ran"
                ctx.tracker.record(
                    tool=call.name,
                    round_no=round_no,
                    state=state_label,
                    input_payload={"blocked": True, "protocol": "cancelled"},
                    error=error,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps({"error": error}, ensure_ascii=False),
                    }
                )
            _write_snapshot(turn_no)
            raise TaskCancelled(f"cancelled after reply at turn {turn_no} boundary")

        if complete_on_text:
            violation = _protocol_violation(response)
            if violation == "":
                # 本阶段的正常出口就是这一段文本。**刻意不去算 patch_applied**:计划段没有任何
                # 写工具,"没有补丁"是构造事实而不是观测;而 working_tree_diff 在非 git 工作区
                # 会抛 GitCmdError(M10 的教训)——合规回复不该被现场条件炸成阶段异常。
                delete_loop_snapshot(ctx, task_id=ctx.task_id)
                log.info(
                    "task %s %s stage completed by text at turn %s",
                    ctx.task_id,
                    state_label,
                    turn_no,
                )
                return LoopOutcome(
                    success=True,
                    summary=(response.content or "").strip(),
                    turns=turn_no,
                    tokens_used=tokens_spent,
                    patch_applied=False,
                    finish_declared=False,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
                    completion="text",
                )
            if violation != "legacy_finish":
                messages.append(_assistant_payload(response))
                for call in response.tool_calls:
                    error = f"tool {call.name!r} is not allowed in phase {state_label}"
                    # 不合协议的工具调用**不进执行**,一条副作用都不落,只留一条被挡的取证事件
                    ctx.tracker.record(
                        tool=call.name,
                        round_no=round_no,
                        state=state_label,
                        input_payload={"blocked": True, "protocol": "text_completion"},
                        error=error,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"error": error}, ensure_ascii=False),
                        }
                    )
                if repairs_used >= MAX_PROTOCOL_REPAIRS:
                    # 修正额度用尽 → 结构化失败,交上层落降级事件。快照照常作废:这一阶段
                    # 已经终结,留着它只会让续跑把同一段死协议再重放一遍
                    delete_loop_snapshot(ctx, task_id=ctx.task_id)
                    return LoopOutcome(
                        success=False,
                        summary=last_content,
                        turns=turn_no,
                        tokens_used=tokens_spent,
                        patch_applied=False,
                        finish_declared=False,
                        tokens_prompt=tokens_prompt,
                        tokens_completion=tokens_completion,
                        completion="protocol_invalid",
                        protocol_reason=violation,
                    )
                repairs_used += 1
                ctx.tracker.record(
                    tool="protocol_repair",
                    round_no=round_no,
                    state=state_label,
                    input_payload={"reason": violation, "turn": turn_no},
                    output_summary={
                        "repairs_used": repairs_used,
                        "max_repairs": MAX_PROTOCOL_REPAIRS,
                        "side_effects": 0,
                    },
                )
                messages.append({"role": "user", "content": TEXT_COMPLETION_REPAIR})
                _write_snapshot(turn_no)
                continue

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
        session = getattr(ctx, "session", None)
        if session is not None:
            session.note_turn(turn_no)
            for call in response.tool_calls:
                session.declare_call(
                    call.id, call.name, stage=state_label, round_no=round_no, turn_no=turn_no
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
            # 有界视图(G05):裁剪发生在**序列化之前、数据结构上**,所以发出去的一定是合法
            # JSON;被裁掉的原文同一次落进任务证据仓,模型用 read_evidence 分页取回。
            # 此前这里是 `json.dumps(...)` 之后再 fold_output —— 单行 JSON 让头尾折叠立刻退化
            # 成 8000 字符硬切,切点落在字符串中间,而丢掉的那段从来没落过盘(PROGRESS D.5)。
            content = json.dumps(model_payload(result), ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if session is not None:
                # 被阶段白名单挡下的调用同样有回执(否则它就是一条孤儿声明)
                session.answer_call(call.id)

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
