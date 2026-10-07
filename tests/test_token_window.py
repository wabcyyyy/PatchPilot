"""M1 上下文压缩纯函数用例:pinned 区、存根内容、Pass A/B 边界与消息形态不变量。

纯函数、零 LLM、零文件系统——所有断言都落在这个消息列表数据结构上。
阈值/期望值是按 `messages_tokens`(len//4)算出来的,不是猜的。
"""

from __future__ import annotations

import copy
import json

from app.context.token_window import REASONING_STUB, STUB_PREFIX, compact_messages
from app.llm.base import messages_tokens

SYSTEM_TEXT = "s" * 400
ISSUE_TEXT = "i" * 200
HEAD = [{"role": "system", "content": SYSTEM_TEXT}, {"role": "user", "content": ISSUE_TEXT}]


def _turn(index: int, big: int, n_tools: int) -> list[dict[str, object]]:
    """一个回合组:assistant(带思维链与工具调用)+ 它的 n_tools 条 tool 回执。"""
    calls = [f"call_{index}_{k}" for k in range(n_tools)]
    assistant: dict[str, object] = {
        "role": "assistant",
        "content": "",
        "reasoning_content": f"推理{index}:" + "r" * 400,
        "tool_calls": [
            {
                "type": "function",
                "id": call_id,
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps(
                        {"path": f"src/mod{index}.py", "offset": index}, ensure_ascii=False
                    ),
                },
            }
            for call_id in calls
        ],
    }
    tools = [{"role": "tool", "tool_call_id": call_id, "content": "x" * big} for call_id in calls]
    return [assistant, *tools]


def _history(turns: int, *, big: int = 4000, n_tools: int = 1) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = list(HEAD)
    for index in range(1, turns + 1):
        messages.extend(_turn(index, big, n_tools))
    return messages


def _assert_tool_replies_bound_to_assistant(messages: list[dict[str, object]]) -> None:
    """不变量:每条 tool 回执都挂在"紧邻它那组"的 assistant 的 tool_calls 里。

    严格端点(DeepSeek 等)对孤儿 tool 消息直接 400——Pass B 整组丢弃必须守住这条。
    """
    open_ids: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            open_ids = {str(call["id"]) for call in calls}  # type: ignore[index]
        elif role == "tool":
            assert str(msg.get("tool_call_id")) in open_ids, f"孤儿 tool 消息: {msg}"
            open_ids.remove(str(msg.get("tool_call_id")))


def _stub_indices(result_messages: list[dict[str, object]]) -> list[int]:
    return [
        index
        for index, msg in enumerate(result_messages)
        if msg.get("role") == "tool" and str(msg.get("content", "")).startswith(STUB_PREFIX)
    ]


def test_disabled_returns_input_unchanged_without_mutation() -> None:
    messages = _history(6)
    snapshot = copy.deepcopy(messages)

    result = compact_messages(messages, max_context_tokens=0, keep_recent_turns=2)

    assert result.already_within is True
    assert result.stubbed == 0 and result.dropped == 0
    assert result.messages == snapshot
    assert result.messages is not messages
    assert result.before_tokens == result.after_tokens == messages_tokens(snapshot)
    assert messages == snapshot  # 入参未被就地修改


def test_negative_threshold_is_also_disabled() -> None:
    result = compact_messages(_history(4), max_context_tokens=-1, keep_recent_turns=2)
    assert result.already_within is True
    assert result.after_tokens == result.before_tokens


def test_already_within_budget_changes_nothing() -> None:
    messages = _history(3)
    snapshot = copy.deepcopy(messages)

    result = compact_messages(messages, max_context_tokens=10**6, keep_recent_turns=2)

    assert result.already_within is True
    assert result.messages == snapshot


def test_pinned_head_survives_hardest_compaction() -> None:
    messages = _history(8)
    result = compact_messages(messages, max_context_tokens=1, keep_recent_turns=1)

    assert result.messages[:2] == HEAD
    assert all(not str(m.get("content", "")).startswith(STUB_PREFIX) for m in result.messages[:2])
    _assert_tool_replies_bound_to_assistant(result.messages)


def test_keep_recent_turns_pins_tail_byte_identical() -> None:
    keep = 3
    messages = _history(8)
    tail = messages[-keep * 2 :]

    result = compact_messages(messages, max_context_tokens=1, keep_recent_turns=keep)

    assert result.messages[-keep * 2 :] == tail  # 逐字节相同,含思维链与原工具结果
    assert result.messages == [*HEAD, *tail]


def test_pass_a_stops_as_soon_as_estimate_is_within_budget() -> None:
    """8,300,2):每条回执 75 tok(>200 字符才会被存根化),阈值 1800 只需压 2 条。"""
    messages = _history(8, big=300, n_tools=2)

    result = compact_messages(messages, max_context_tokens=1800, keep_recent_turns=2)

    assert result.stubbed == 2
    assert result.dropped == 0
    assert result.after_tokens <= 1800
    stubs = _stub_indices(result.messages)
    assert stubs == [3, 4]  # 最旧那组的两条回执,而不是最新的
    assert result.messages[25]["content"] == "x" * 300  # 未处理的组原样保留


def test_stub_retains_tool_name_and_args_so_model_knows_it_already_read_it() -> None:
    messages = _history(8, big=300, n_tools=2)
    result = compact_messages(messages, max_context_tokens=1500, keep_recent_turns=2)

    stub = str(result.messages[3]["content"])
    assert stub.startswith(STUB_PREFIX)
    assert "tool=read_file" in stub  # 工具名:让模型知道"这条路已经走过"
    assert "src/mod1.py" in stub and "offset=1" in stub  # 关键入参
    assert "orig_chars=300" in stub
    assert "result omitted from working memory" in stub
    assert "\n" not in stub  # 单行
    assert "xxx" not in stub  # 原结果不再在场
    assert result.messages[3]["role"] == "tool"
    assert result.messages[3]["tool_call_id"] == messages[3]["tool_call_id"]
    assert result.messages[4]["content"].startswith(f"{STUB_PREFIX} tool=read_file")


def test_short_tool_results_are_not_stubbed() -> None:
    """短结果压了不划算(存根本身占一行):只能靠整组丢弃。"""
    result = compact_messages(_history(6, big=80), max_context_tokens=1, keep_recent_turns=1)

    assert result.stubbed == 0
    assert result.dropped == 10


def test_reasoning_is_stubbed_but_stays_present() -> None:
    """思考模式端点缺 reasoning_content 就 400:字段只能压小,不能删。"""
    keep = 2
    messages = _history(8, big=300, n_tools=2)

    result = compact_messages(messages, max_context_tokens=1500, keep_recent_turns=keep)

    assistants = [m for m in result.messages if m.get("role") == "assistant"]
    assert len(assistants) == 8
    assert all("reasoning_content" in m for m in assistants)
    stubbed = [m for m in assistants if m["reasoning_content"] == REASONING_STUB]
    assert stubbed and len(stubbed) <= 8 - keep
    for msg in assistants[len(assistants) - keep :]:  # pinned 尾部原样
        assert msg["reasoning_content"] != REASONING_STUB


def test_pass_b_drops_whole_groups_and_never_orphans_tool_messages() -> None:
    messages = _history(8, big=4000, n_tools=3)

    result = compact_messages(messages, max_context_tokens=7000, keep_recent_turns=2)

    assert result.stubbed == 18  # Pass A 先压干可压区
    assert result.dropped == 12  # 仍超量才整组丢(4 组 × 3 条)
    assert result.after_tokens <= 7000
    assert len(result.messages) == len(messages) - 12
    _assert_tool_replies_bound_to_assistant(result.messages)


def test_unreachable_target_returns_smallest_achievable_result() -> None:
    """阈值低于 pinned 区尺寸时压不到目标:返回最小结果,门禁仍由上层执行。"""
    keep = 2
    messages = _history(8, big=4000, n_tools=1)

    result = compact_messages(messages, max_context_tokens=1, keep_recent_turns=keep)

    assert result.stubbed == 6 and result.dropped == 12
    assert result.after_tokens > 1  # 压无可压也绝不假装达标
    assert result.messages == [*HEAD, *messages[-keep * 2 :]]
    _assert_tool_replies_bound_to_assistant(result.messages)


def test_no_compactable_region_when_groups_below_keep_recent() -> None:
    messages = _history(2)
    snapshot = copy.deepcopy(messages)

    result = compact_messages(messages, max_context_tokens=10, keep_recent_turns=5)

    assert result.already_within is False
    assert result.stubbed == 0 and result.dropped == 0
    assert result.messages == snapshot
    assert result.after_tokens == result.before_tokens


def test_input_list_is_never_mutated_when_compaction_runs() -> None:
    messages = _history(8, big=300, n_tools=2)
    snapshot = copy.deepcopy(messages)

    compact_messages(messages, max_context_tokens=1200, keep_recent_turns=2)

    assert messages == snapshot


def test_deterministic_for_identical_input() -> None:
    messages = _history(7, big=300, n_tools=2)
    kwargs = {"max_context_tokens": 1500, "keep_recent_turns": 3}
    first = compact_messages(messages, **kwargs)  # type: ignore[arg-type]
    second = compact_messages(copy.deepcopy(messages), **kwargs)  # type: ignore[arg-type]

    assert first.messages == second.messages
    assert (first.stubbed, first.dropped) == (second.stubbed, second.dropped)
    assert (first.before_tokens, first.after_tokens) == (
        second.before_tokens,
        second.after_tokens,
    )


def test_token_estimates_use_the_shared_estimator() -> None:
    messages = _history(6, big=4000, n_tools=1)

    result = compact_messages(messages, max_context_tokens=1800, keep_recent_turns=2)

    assert result.before_tokens == messages_tokens(messages)
    assert result.after_tokens == messages_tokens(result.messages)
    assert result.after_tokens < result.before_tokens
