"""工作记忆的滑动窗口压缩:纯函数、确定性、零 LLM 调用、不碰文件系统。

缺陷出处:消息列表只有一个构建点且**从不裁剪**(见 PROGRESS.md D.2),上下文只增不减,
超预算的唯一出口是 BudgetError——实测多文件题光靠只读调查就烧到 417,894 tokens
(runs/swe-hard-graph*)而一次补丁都不提。这里把"中止"换成"压缩后继续":
- 不可压区:第 0 条 system、首条 user、最近 keep_recent_turns 个回合组,一字不动;
- Pass A:更早的 tool 结果换成单行存根,**保留工具名与关键入参**——只删内容不告诉模型
  "查过什么",等于教它把同一个文件再读一遍;仍超量才有 Pass B:整组丢弃最旧回合,
  assistant 与它的 tool 回执同进同出,否则严格端点(DeepSeek 等)因孤儿消息直接 400。

压缩只缩小"发给模型的内容",不参与判定:预算/门禁语义一字未改,压完仍超预算照旧抛
BudgetError。token 估算一律用 `app.llm.base.messages_tokens`,与循环里同一个口径。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.llm.base import messages_tokens

log = logging.getLogger(__name__)

STUB_PREFIX = "[compacted]"  # 用例与轨迹都靠它识别"这条已经被压过"
REASONING_STUB = "[compacted reasoning]"
STUB_MIN_TOOL_CHARS = 200  # 短结果压了不划算(存根自己就占一行)
STUB_ARG_CHARS = 60  # args= 段上限:够写一个路径/关键字,不会把补丁正文抄回来
ARGS_PLACEHOLDER = "(args omitted)"


@dataclass(frozen=True)
class CompactionResult:
    """一次压缩的原始事实。`messages` 是新列表,入参永不被就地修改。"""

    messages: list[dict[str, object]]
    before_tokens: int
    after_tokens: int
    stubbed: int  # 换成存根的 tool 结果条数(思维链存根不计入)
    dropped: int  # 被整组丢弃的消息条数
    already_within: bool  # True = 未做任何压缩(功能关闭,或本来就没超)


def _role(msg: dict[str, object]) -> str:
    return str(msg.get("role") or "")


def _regions(messages: list[dict[str, object]]) -> tuple[int, list[list[int]]]:
    """返回 (不可压头部结束下标, 头部之后的回合组下标列表)。

    一条 assistant/user 开启一组,其后的 tool 归入该组;孤儿 tool 自成一组,保证任何
    输入都被完整覆盖。头部 = 第 0 条 system + 首条 user(含它们之前的一切)。
    """
    head_end = 1 if messages and _role(messages[0]) == "system" else 0
    for index in range(head_end, len(messages)):
        if _role(messages[index]) == "user":
            head_end = index + 1
            break
    groups: list[list[int]] = []
    for index in range(head_end, len(messages)):
        if _role(messages[index]) in ("assistant", "user") or not groups:
            groups.append([index])
        else:
            groups[-1].append(index)
    return head_end, groups


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _short_args(raw: object) -> str:
    """参数摘要:按 key=value 顺序取前几项,总长 <= STUB_ARG_CHARS。"""
    args: Any = raw
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except (ValueError, TypeError):
            return raw[:STUB_ARG_CHARS] or ARGS_PLACEHOLDER
    if not isinstance(args, dict):
        return ARGS_PLACEHOLDER if args is None else str(args)[:STUB_ARG_CHARS]
    parts: list[str] = []
    used = 0
    for key, value in args.items():
        piece = f"{key}={_as_text(value)[:STUB_ARG_CHARS]}"
        if used + len(piece) + 2 > STUB_ARG_CHARS:
            break
        parts.append(piece)
        used += len(piece) + 2
    return ", ".join(parts)[:STUB_ARG_CHARS] or ARGS_PLACEHOLDER


def _tool_call_index(messages: list[dict[str, object]]) -> dict[str, tuple[str, str]]:
    """tool_call_id → (工具名, 参数摘要),从 assistant 消息的 tool_calls 反查。

    线格式是 OpenAI 标准(`plain_loop.py` 回填处):arguments 为 JSON 字符串;兼容 dict
    形态,免得调用方传未序列化的结构时把"查过什么"这些信息丢掉。
    """
    index: dict[str, tuple[str, str]] = {}
    for msg in messages:
        calls = msg.get("tool_calls")
        if _role(msg) != "assistant" or not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function")
            source: dict[str, object] = function if isinstance(function, dict) else call
            if call_id:
                index[call_id] = (
                    str(source.get("name") or "?"),
                    _short_args(source.get("arguments")),
                )
    return index


def _stub(name: str, arg_text: str, orig_chars: int) -> str:
    return (
        f"{STUB_PREFIX} tool={name} args={arg_text} orig_chars={orig_chars} "
        "— result omitted from working memory"
    )


def _build(
    items: list[dict[str, object]],
    removed: set[int],
    before: int,
    stubbed: int,
    dropped: int,
    *,
    already_within: bool = False,
) -> CompactionResult:
    out = [msg for index, msg in enumerate(items) if index not in removed]
    after = messages_tokens(out)
    if stubbed or dropped:
        log.debug(
            "context compact: %d -> %d tokens (stubbed=%d dropped=%d messages=%d)",
            before,
            after,
            stubbed,
            dropped,
            len(out),
        )
    return CompactionResult(out, before, after, stubbed, dropped, already_within)


def compact_messages(
    messages: list[dict[str, object]],
    *,
    max_context_tokens: int,
    keep_recent_turns: int,
) -> CompactionResult:
    """按软阈值压缩工作记忆;关闭(max_context_tokens <= 0)时原样返回。

    两趟按序执行,估算一旦 <= 目标就停:Pass A 把可压区的 tool 结果与思维链存根化,
    Pass B 再从最旧开始整组丢弃。两趟压干仍超量就返回最小可达结果,不假装达标:
    上层既有 BudgetError 门禁照旧生效。
    """
    before = messages_tokens(messages)
    target = max_context_tokens
    if max_context_tokens <= 0 or before <= max_context_tokens:
        return _build(messages, set(), before, 0, 0, already_within=True)

    _, groups = _regions(messages)
    pinned_tail = max(1, keep_recent_turns)  # 至少钉住最近一组,否则尾部也保不住
    if len(groups) <= pinned_tail:
        return _build(messages, set(), before, 0, 0)  # 可压区为空:压不动,原样返回

    working = list(messages)
    removed: set[int] = set()
    calls = _tool_call_index(messages)
    compactable = groups[: len(groups) - pinned_tail]
    stubbed = 0

    def estimate() -> int:
        return messages_tokens([m for i, m in enumerate(working) if i not in removed])

    for group in compactable:  # Pass A:最旧优先
        for index in group:
            msg = working[index]
            if _role(msg) == "tool":
                content = msg.get("content")
                if isinstance(content, str) and len(content) > STUB_MIN_TOOL_CHARS:
                    name, arg_text = calls.get(str(msg.get("tool_call_id") or ""), ("?", ""))
                    working[index] = {**msg, "content": _stub(name, arg_text, len(content))}
                    stubbed += 1
            elif _role(msg) == "assistant":
                reasoning = msg.get("reasoning_content")
                # 字段必须"存在但压小",不能删:思考模式端点缺它就 400
                if isinstance(reasoning, str) and reasoning and reasoning != REASONING_STUB:
                    working[index] = {**msg, "reasoning_content": REASONING_STUB}
        if estimate() <= target:
            return _build(working, removed, before, stubbed, 0)

    dropped = 0
    for group in compactable:  # Pass B:仍超量才整组丢(同组同进同出,不留孤儿 tool)
        for index in group:
            if index not in removed:
                removed.add(index)
                dropped += 1
        if estimate() <= target:
            break

    return _build(working, removed, before, stubbed, dropped)
