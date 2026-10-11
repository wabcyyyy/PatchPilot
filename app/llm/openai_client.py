"""OpenAI 兼容端点的模型封装(懒加载 openai 包,未配置时不可用)。

计量协议(G02)在这里落地,顺序是硬性的:

1. **先解析 usage**——端点返回了什么消耗,在任何结构解释之前就已经定下来;
2. 再解释 choices / 工具参数 / finish_reason,解释不了一切都算"解析失败",
   但失败必须带着**已知消耗**一起抛出(`ModelResponseError`/`ModelTransportError`),
   由循环先入账再收尾(验收矩阵 T03:此前 usage 随异常一起蒸发,花了钱不入账);
3. 每次**物理** attempt 有独立 ID、自己的用量读数与失败状态。SDK 的隐式重试
   (`llm_max_retries > 0`)发生在一发 `create()` 内部,平台看不见次数,只能如实记
   `retry_owner=sdk_opaque` 与上界——不补造历史用量。要换成平台显式重试,必须把
   SDK 的重试上界设为 0,否则两层重试会让用量对不上(构造处直接拒绝)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.errors import ModelResponseError, ModelTransportError, TaskError
from app.llm.base import AssistantTurn, ToolCall, estimate_tokens, messages_tokens
from app.llm.profiles import profile_from_settings
from app.llm.usage import (
    ATTEMPT_OK,
    ATTEMPT_PARSE_FAILED,
    ATTEMPT_TRANSPORT_FAILED,
    RETRY_PLATFORM,
    RETRY_SDK_OPAQUE,
    AttemptRecord,
    ModelResponseEnvelope,
    UsageEnvelope,
    estimate_request,
    new_attempt_id,
    parse_usage,
)

log = logging.getLogger(__name__)

# 只有传输类故障值得原样重发。内容型错误(400 的 reasoning 协议错、超长输入)重发
# 只是再花一遍钱,还会把"端点明确拒绝"糊成"网络不稳"。
_RETRYABLE_TRANSPORT_ERRORS = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }
)


def _is_retryable_transport(exc: BaseException) -> bool:
    return any(candidate.__name__ in _RETRYABLE_TRANSPORT_ERRORS for candidate in type(exc).__mro__)


@dataclass(frozen=True)
class TransportRetryPolicy:
    """平台显式重试:`max_extra_attempts` 次**额外**物理尝试,每次都单独计量。

    默认 0 = 与既有行为逐字相同(一次就交差,重试由 SDK 的上界决定)。非 0 只允许在
    `llm_max_retries=0` 时使用——这就是 SPEC 的"repair_v2 禁 SDK 隐式重试,由平台显式
    retry"的执行点。生产装配点在构造处:不显式传 policy 时读 `Settings.llm_platform_retries`
    (G07a 把 G02 留下的"无调用方"开关接上;默认 0 因此与今日同形)。
    """

    max_extra_attempts: int = 0

    def __post_init__(self) -> None:
        if self.max_extra_attempts < 0:
            raise ValueError(f"max_extra_attempts must be >= 0, got {self.max_extra_attempts}")


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

    def __init__(
        self,
        settings: Settings,
        *,
        retry_policy: TransportRetryPolicy | None = None,
    ) -> None:
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
        # 能力档案(G07):在发出第一个请求之前把"这个端点被声明成能做什么"钉下来。
        # 未声明项走保守档并在报告里列出假设,申报比保守档大(且没有来源)直接拒绝发起。
        # 冻结实例,随客户端一起活过整个任务:受理之后改环境变量不会再影响已经在跑的任务。
        self.profile = profile_from_settings(settings)
        self.profile.assert_usable_for_repair()
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise TaskError("openai package not installed; pip install 'patchpilot[llm]'") from exc
        policy = retry_policy or TransportRetryPolicy(
            max_extra_attempts=int(getattr(settings, "llm_platform_retries", 0) or 0)
        )
        if policy.max_extra_attempts and settings.llm_max_retries > 0:
            # 两层重试 = 同一笔消耗看不见、也算不清;宁可拒绝配置,不做"看起来能开"的开关。
            raise TaskError(
                "platform transport retries require PATCHPILOT_LLM_MAX_RETRIES=0;"
                f" got llm_max_retries={settings.llm_max_retries}"
                "(SDK 与平台同时重试会让用量无法对账)"
            )
        self._client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._max_tokens = settings.llm_max_tokens
        self.model_name = settings.llm_model
        self._thinking = settings.llm_thinking
        self._extra_body = _build_extra_body(settings.llm_thinking)
        self._sdk_retry_bound = settings.llm_max_retries
        # 重试归属:SDK 上界非 0 时,平台连"发出去几次"都看不见,只能如实标 sdk_opaque。
        self._retry_owner = RETRY_PLATFORM if settings.llm_max_retries == 0 else RETRY_SDK_OPAQUE
        self._attempt_budget = 1 + policy.max_extra_attempts

    def _envelope(
        self,
        attempts: list[AttemptRecord],
        *,
        finish_reason: str,
        usage: UsageEnvelope,
        request_estimate: Any,
        parse_failures: list[str] | None = None,
        truncated: bool = False,
        reasoning_returned: bool = False,
        served_model: str = "",
    ) -> ModelResponseEnvelope:
        return ModelResponseEnvelope(
            usage=usage,
            finish_reason=finish_reason,
            attempts=list(attempts),
            parse_failures=list(parse_failures or []),
            thinking_requested=self._thinking,
            reasoning_returned=reasoning_returned,
            truncated=truncated,
            request_estimate=request_estimate,
            requested_model=self.model_name,
            served_model=served_model,
        )

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,  # type: ignore[arg-type]
            "tools": [{"type": "function", "function": tool} for tool in tools] if tools else None,  # type: ignore[arg-type]
            "max_tokens": self._max_tokens or None,
            "extra_body": self._extra_body,
        }
        estimate = estimate_request(messages, tools)
        attempts: list[AttemptRecord] = []

        for index in range(1, self._attempt_budget + 1):
            attempt = AttemptRecord(
                index=index,
                attempt_id=new_attempt_id(),
                status=ATTEMPT_OK,
                usage=UsageEnvelope.unknown("not_started"),
                retry_owner=self._retry_owner,
                sdk_retry_bound=self._sdk_retry_bound,
            )
            attempts.append(attempt)
            try:
                response = self._client.chat.completions.create(**request)
            except Exception as exc:  # 传输层什么都可能抛(SDK/网络/超时),分类后如实上抛
                attempt.status = ATTEMPT_TRANSPORT_FAILED
                attempt.error_class = type(exc).__name__
                attempt.usage = UsageEnvelope.unknown(type(exc).__name__)
                if index < self._attempt_budget and _is_retryable_transport(exc):
                    log.warning(
                        "llm transport failure %s on attempt %s; platform retrying",
                        type(exc).__name__,
                        index,
                    )
                    continue
                if self._attempt_budget == 1:
                    # 没有平台重试可报告时原样上抛:循环会把这次调用记成 unknown,
                    # 异常类型与既有映射逐字相同(不在此把 APIConnectionError 换成 TaskError)
                    raise
                raise ModelTransportError(
                    f"llm transport failed after {len(attempts)} physical attempt(s):"
                    f" {type(exc).__name__}: {exc}",
                    envelope=self._envelope(
                        attempts,
                        finish_reason="",
                        usage=UsageEnvelope.combine([a.usage for a in attempts]),
                        request_estimate=estimate,
                        parse_failures=["transport_failed"],
                    ),
                ) from exc

            # ① 用量先于一切业务解释:这一步之后,任何失败路径都还能查到"这次花了多少"
            usage = parse_usage(getattr(response, "usage", None))
            if not usage.has_known_usage:
                # R2 整改的字符估算回退保留(读数算法一字未动),但来源如实降为 estimated
                prompt_tokens = messages_tokens(messages)
                completion_tokens = estimate_tokens(_reply_text_of(response))
                usage = UsageEnvelope.estimated(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    reason="endpoint_omitted_usage",
                )
            attempt.usage = usage
            # 身份观测(G07):端点回报的模型名。名字相同**不**证明服务端版本相同(§G04
            # 的 alias_proves_served_version=false),但名字与请求名不同就是已经发生的漂移,
            # 这条事实要留在 envelope 里,而不是被"看起来没问题"吞掉。
            served_model = str(getattr(response, "model", "") or "")
            if served_model and served_model != self.model_name:
                log.warning(
                    "llm served model %r differs from requested %r(端点换了实际模型)",
                    served_model,
                    self.model_name,
                )
            envelope = self._envelope(
                attempts,
                finish_reason="",
                usage=UsageEnvelope.combine([a.usage for a in attempts]),
                request_estimate=estimate,
                served_model=served_model,
            )

            # ② 结构解释:空 choices / 截断 / 坏工具参数都归类成解析失败,但异常带着 envelope
            choice = response.choices[0] if getattr(response, "choices", None) else None
            if choice is None:
                # R2 整改:空 choices(部分端点在内容过滤/故障时返回)收敛为结构化错误,
                # 而不是 IndexError 被上层吞成 NEEDS_REVIEW;G02 补的是"已知用量随异常交付"
                attempt.status = ATTEMPT_PARSE_FAILED
                envelope.parse_failures.append("choices_missing")
                raise ModelResponseError(
                    "llm endpoint returned no choices(usage 已入账,结构无法解释)",
                    envelope=envelope,
                )
            message = choice.message
            finish_reason = choice.finish_reason or "stop"
            envelope.finish_reason = finish_reason

            calls, invalid_arguments = _parse_tool_calls(message)

            text = getattr(message, "content", None)
            reasoning = getattr(message, "reasoning_content", None)
            envelope.reasoning_returned = bool(reasoning)
            if finish_reason == "length" and calls:
                # R2 整改:截断的 tool arguments 多半是废补丁,早失败好过假进行
                # (复盘 P2 对齐注释与行为:此前只告警,坏参数仍会流进 apply);
                # 截断的补丁**不得执行**,且这一条的花费必须已经入账(T03)。
                # 此时的 arguments 解析失败是截断的**后果**,只登记致命那条原因
                attempt.status = ATTEMPT_PARSE_FAILED
                envelope.truncated = True
                envelope.parse_failures.append("truncated_tool_arguments")
                raise ModelResponseError(
                    "llm completion truncated by max_tokens while tool calls were pending;"
                    " increase llm_max_tokens or reduce prompt size",
                    envelope=envelope,
                )
            if finish_reason == "length":
                # 纯文本截断无工具调用时维持告警——文本被截不必然致命,但要显式归类
                envelope.truncated = True
                envelope.parse_failures.append("truncated_completion")
                log.warning("llm completion truncated by max_tokens (text only)")
            envelope.parse_failures.extend(["tool_arguments_invalid"] * invalid_arguments)

            return AssistantTurn(
                content=text,
                tool_calls=calls,
                finish_reason=finish_reason,
                usage_tokens=int(envelope.usage.chargeable_total or 0),
                prompt_tokens=int(envelope.usage.prompt_tokens or 0),
                completion_tokens=int(envelope.usage.completion_tokens or 0),
                # 思考模式的思维链必须原样回传(见 AssistantTurn.reasoning_content)
                reasoning_content=reasoning,
                envelope=envelope,
            )

        # 循环里每一轮都以 return 或 raise 收场;这条路径走不到,写出来是为了 RET503
        # 不留"隐式返回 None"的空隙(将来插一个 continue 分支就不会静默变成坏形状)
        raise TaskError("no physical attempt produced a response")  # pragma: no cover


def _parse_tool_calls(message: Any) -> tuple[list[ToolCall], int]:
    """把工具调用解析成 (calls, 无法解析的参数条数)。

    无法解析的参数保留原始串 `_raw` 交回循环(工具层会因缺字段而失败,给模型一条可读
    反馈),但**归类必须留痕**——此前坏 JSON 悄悄变成一个正常回合,轨迹里看不出模型
    其实给了一个读不出来的动作。截断导致的解析失败由 truncated 分支单独归类。
    """
    calls: list[ToolCall] = []
    invalid = 0
    for item in getattr(message, "tool_calls", None) or []:
        fn = item.function
        raw_arguments = fn.arguments or "{}"
        try:
            args: Any = json.loads(raw_arguments)
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            args = {"_raw": raw_arguments}
            invalid += 1
        calls.append(ToolCall(id=item.id or f"call_{len(calls)}", name=fn.name, arguments=args))
    return calls, invalid


def _reply_text_of(response: Any) -> str:
    """只为估算服务:取回复文本,不做任何"这一步意味着什么"的解释。"""
    choice = response.choices[0] if getattr(response, "choices", None) else None
    if choice is None:
        return ""
    message = choice.message
    text = getattr(message, "content", None) or ""
    if calls := getattr(message, "tool_calls", None):
        return text + json.dumps(
            [getattr(c.function, "arguments", "") for c in calls], ensure_ascii=False, default=str
        )
    return text


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
