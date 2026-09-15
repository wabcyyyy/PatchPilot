"""FakeLLM:脚本化回放模型。

按预置脚本依次吐出工具调用,用于:
1. 离线确定性测试(不联网、不烧 token);
2. 平台闭环验证(replay 模式:脚本由题目目录提供,验证"定位→补丁→测试"全链路)。
回放模式明确标注 provider=fake-replay,不冒充真实模型成绩。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.base import AssistantTurn, ToolCall, estimate_tokens

log = logging.getLogger(__name__)

Step = dict[str, Any]


class FakeLLM:
    """按脚本顺序回放;脚本耗尽后返回 success=false 的 finish(防御死循环)。"""

    provider = "fake-replay"

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self._cursor = 0

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        if self._cursor >= len(self.script):
            log.warning("FakeLLM script exhausted; returning failure finish")
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id=f"call_exhausted_{self._cursor}",
                        name="finish",
                        arguments={"success": False, "summary": "replay script exhausted"},
                    )
                ],
                finish_reason="tool_calls",
            )

        step = self.script[self._cursor]
        self._cursor += 1
        usage = estimate_tokens(json.dumps(step, ensure_ascii=False))

        if "content" in step:
            return AssistantTurn(
                content=str(step["content"]), finish_reason="stop", usage_tokens=usage
            )

        name = str(step["tool"])
        args = dict(step.get("args", {}))
        return AssistantTurn(
            tool_calls=[ToolCall(id=f"call_{self._cursor}", name=name, arguments=args)],
            finish_reason="tool_calls",
            usage_tokens=usage,
        )

    @classmethod
    def from_json_file(cls, path) -> "FakeLLM":
        import pathlib

        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return cls(script=data if isinstance(data, list) else data.get("steps", []))
