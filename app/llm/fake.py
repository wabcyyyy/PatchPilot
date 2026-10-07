"""FakeLLM:脚本化回放模型。

按预置脚本依次吐出工具调用,用于:
1. 离线确定性测试(不联网、不烧 token);
2. 平台闭环验证(replay 模式:脚本由题目目录提供,验证"定位→补丁→测试"全链路)。
回放模式明确标注 provider=fake-replay,不冒充真实模型成绩。

例外:带 PLAN_MARKER 的计划请求走**非消耗**通道(见 _plan_request)——脚本步数与
引入 PLAN 阶段之前逐字一致,既有回放脚本无需改动。真实模型没有这一步骤差异,
它只是多收到一次请求。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.base import AssistantTurn, ToolCall, estimate_tokens
from app.prompts import PLAN_MARKER

log = logging.getLogger(__name__)

Step = dict[str, Any]


def _plan_request(messages: list[dict[str, Any]]) -> str:
    """PLAN 阶段请求的自识别:命中 marker 时返回那条用户消息全文,否则返回空串。

    为什么必须有这条通道:FakeLLM 回放的是**固定脚本**,每次 complete() 弹一步;
    tests/ 里的 graph 用例都写成 `_localize_script() + _propose_script(...)`。M5 插入 PLAN
    阶段后,计划请求若也弹一步,那 20 多个用例的脚本会整体错位——而"顺手改脚本"会把
    错位掩盖成通过。计划请求因此**不消耗脚本**:它只需要产出一段计划文本,内容由请求
    里的定位结论确定性推出,真实的 LOCALIZE→PLAN→PROPOSE 序列仍可整段回放。
    """
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and PLAN_MARKER in content:
            return content
    return ""


def _plan_text(request: str) -> str:
    """从计划请求里取"定位阶段结论"的第一行做占位计划;绝不回含 marker(否则会污染后续请求)。"""
    lines = [line.strip() for line in request.splitlines() if line.strip()]
    anchor = ""
    for index, line in enumerate(lines):
        if line.startswith("### 定位阶段结论") and index + 1 < len(lines):
            anchor = lines[index + 1][:60]
            break
    return f"计划:按定位结论({anchor})改最小面,再按 failed/regression 双集验证。"


class FakeLLM:
    """按脚本顺序回放;脚本耗尽后返回 success=false 的 finish(防御死循环)。"""

    provider = "fake-replay"

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self._cursor = 0

    @property
    def consumed(self) -> int:
        """已消耗的脚本步数:用例据此证明计划阶段没有动过游标(M5 的兼容性契约)。"""
        return self._cursor

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        request = _plan_request(messages)
        if request:
            # 计划请求:直接给"一段计划文本 + finish"的回合,不移动 _cursor。
            # 带 finish 是为了让 plan 节点的循环在**第一个 turn**收敛——纯文本回合会让
            # run_plain_loop 继续下一轮,那样就会真的弹脚本步。
            text = _plan_text(request)
            usage = estimate_tokens(text)
            return AssistantTurn(
                content=text,
                tool_calls=[
                    ToolCall(
                        id=f"call_plan_{self._cursor}",
                        name="finish",
                        arguments={"success": True, "summary": text},
                    )
                ],
                finish_reason="tool_calls",
                usage_tokens=usage,
                prompt_tokens=0,
                completion_tokens=usage,
            )

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
                content=str(step["content"]),
                finish_reason="stop",
                usage_tokens=usage,
                prompt_tokens=0,
                completion_tokens=usage,
            )

        name = str(step["tool"])
        args = dict(step.get("args", {}))
        return AssistantTurn(
            tool_calls=[ToolCall(id=f"call_{self._cursor}", name=name, arguments=args)],
            finish_reason="tool_calls",
            usage_tokens=usage,
            prompt_tokens=0,
            completion_tokens=usage,
        )
