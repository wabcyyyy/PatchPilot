"""OpenAI 兼容端点的模型封装(懒加载 openai 包,未配置时不可用)。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.errors import TaskError
from app.llm.base import AssistantTurn, ToolCall, estimate_tokens

log = logging.getLogger(__name__)


class OpenAICompatModel:
    """任何 OpenAI 兼容 /chat/completions 端点(base_url + api_key + model)。"""

    provider = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key or not settings.llm_base_url:
            raise TaskError(
                "openai provider requires PATCHPILOT_LLM_BASE_URL and PATCHPILOT_LLM_API_KEY"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise TaskError("openai package not installed; pip install 'patchpilot[llm]'") from exc
        self._client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._max_tokens = settings.llm_max_tokens
        self.model_name = settings.llm_model

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=messages,  # type: ignore[arg-type]
            tools=[{"type": "function", "function": tool} for tool in tools] if tools else None,  # type: ignore[arg-type]
            max_tokens=self._max_tokens or None,
        )
        choice = response.choices[0]
        message = choice.message
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) if usage else 0

        calls: list[ToolCall] = []
        for item in getattr(message, "tool_calls", None) or []:
            fn = item.function
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.arguments}
            calls.append(ToolCall(id=item.id or f"call_{len(calls)}", name=fn.name, arguments=args))

        text = getattr(message, "content", None)
        return AssistantTurn(
            content=text,
            tool_calls=calls,
            finish_reason=choice.finish_reason or "stop",
            usage_tokens=int(tokens) or estimate_tokens(text or ""),
        )


def build_model(provider: str, settings: Settings, script: list[dict[str, Any]] | None = None):
    """provider 工厂:fake(需脚本)/ openai(需配置)。"""
    if provider == "fake":
        if script is None:
            raise TaskError("fake provider requires a replay script")
        from app.llm.fake import FakeLLM

        return FakeLLM(script)
    if provider == "openai":
        return OpenAICompatModel(settings)
    raise TaskError(f"unknown model provider: {provider}")
