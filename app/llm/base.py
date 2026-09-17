"""模型抽象:统一 turn 结构与 token 估算,隔离具体 SDK。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantTurn:
    """模型的一次回复:要么带工具调用,要么纯文本。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage_tokens: int = 0
    prompt_tokens: int = 0  # 输入明细;端点未提供时为 0
    completion_tokens: int = 0  # 输出明细;端点未提供时为 0

    @property
    def is_tool_call(self) -> bool:
        return bool(self.tool_calls)


class Model(Protocol):
    """所有模型实现的协议:输入消息与工具 schema,输出一个回合。"""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn: ...


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数(约 4 字符/token),供预算门禁使用。"""
    return max(1, len(text) // 4)


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        if tool_calls := msg.get("tool_calls"):
            total += estimate_tokens(repr(tool_calls))
    return total
