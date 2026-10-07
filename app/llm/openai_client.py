"""OpenAI 兼容端点的模型封装(懒加载 openai 包,未配置时不可用)。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.errors import TaskError
from app.llm.base import AssistantTurn, ToolCall, estimate_tokens, messages_tokens

log = logging.getLogger(__name__)


def _build_extra_body(thinking: str) -> dict[str, Any] | None:
    """DeepSeek 思考模式:disabled 关闭;low/high/max 开启并设强度;空 = 跟随服务端默认。

    走 extra_body 以兼容不同 SDK 版本(参数原样并入请求体)。
    """
    if not thinking:
        return None
    if thinking == "disabled":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": thinking}


class OpenAICompatModel:
    """任何 OpenAI 兼容 /chat/completions 端点(base_url + api_key + model)。"""

    provider = "openai"

    def __init__(self, settings: Settings) -> None:
        # P3-3 整改(P0-2 复活链收口):llm_enabled=true + 凭据齐 + llm_model=""
        # 的组合此前全程零拦截,report.json 会落盘 model_name=""——真实评测
        # 不可追溯。守卫下沉到构造处:任何经 build_model("openai") 的入口
        # (API 预检/plain/graph/run_single)都被同一道校验拦下。
        if not settings.llm_model.strip():
            raise TaskError(
                "PATCHPILOT_LLM_MODEL is empty; real LLM calls must carry a model name"
                "(缺模型名的任务/批次拒绝发起,否则 report.json 无模型身份、证据链断裂)"
            )
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
        self._extra_body = _build_extra_body(settings.llm_thinking)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=messages,  # type: ignore[arg-type]
            tools=[{"type": "function", "function": tool} for tool in tools] if tools else None,  # type: ignore[arg-type]
            max_tokens=self._max_tokens or None,
            extra_body=self._extra_body,
        )
        choice = response.choices[0] if response.choices else None
        if choice is None:
            # R2 整改:空 choices(部分端点在内容过滤/故障时返回)收敛为结构化
            # TaskError,而不是 IndexError 被上层吞成 NEEDS_REVIEW
            raise TaskError("llm endpoint returned no choices")
        message = choice.message
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) if usage else 0
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

        calls: list[ToolCall] = []
        for item in getattr(message, "tool_calls", None) or []:
            fn = item.function
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.arguments}
            calls.append(ToolCall(id=item.id or f"call_{len(calls)}", name=fn.name, arguments=args))

        text = getattr(message, "content", None)
        if choice.finish_reason == "length" and calls:
            # R2 整改:截断的 tool arguments 多半是废补丁,早失败好过假进行
            # (复盘 P2 对齐注释与行为:此前只告警,坏参数仍会流进 apply);
            # 纯文本截断无工具调用时维持告警——文本被截不必然致命
            raise TaskError(
                "llm completion truncated by max_tokens while tool calls were pending;"
                " increase llm_max_tokens or reduce prompt size"
            )
        if choice.finish_reason == "length":
            log.warning("llm completion truncated by max_tokens (text only)")
        # R2 整改:usage 缺失时此前 prompt/completion 恒 0,成本核算系统性失真;
        # 改为字符估算拆分(与 plain_loop 的预算口径同源)
        if not tokens:
            prompt_tokens = messages_tokens(messages)
            completion_tokens = estimate_tokens(
                text or json.dumps([c.arguments for c in calls], ensure_ascii=False, default=str)
            )
            tokens = prompt_tokens + completion_tokens
        return AssistantTurn(
            content=text,
            tool_calls=calls,
            finish_reason=choice.finish_reason or "stop",
            usage_tokens=int(tokens),
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            # 思考模式的思维链必须原样回传(见 AssistantTurn.reasoning_content)
            reasoning_content=getattr(message, "reasoning_content", None),
        )


def build_model(provider: str, settings: Settings, script: list[dict[str, Any]] | None = None):
    """provider 工厂:fake(需脚本)/ openai(需配置+总开关)。"""
    if provider == "fake":
        if script is None:
            raise TaskError("fake provider requires a replay script")
        from app.llm.fake import FakeLLM

        return FakeLLM(script)
    if provider == "openai":
        if not settings.llm_enabled:
            raise TaskError(
                "openai provider is disabled; set PATCHPILOT_LLM_ENABLED=true to allow real LLM calls"
            )
        return OpenAICompatModel(settings)
    raise TaskError(f"unknown model provider: {provider}")
