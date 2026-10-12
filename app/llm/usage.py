"""模型响应与计量协议(G02):usage 的解析排在一切业务解释之前。

缺陷现场(评审 R3 第 3 条 / SPEC §4 契约 2、3,`agent-readiness-review-2026-10-10.md`):
- 客户端先取 choices、后读 usage。结构一坏(空 choices、工具 arguments 不是 JSON、
  被 max_tokens 截断)就抛异常,**端点已经返回的 usage 随异常一起蒸发**,账本里那次
  调用只剩 unknown——钱花了却不入账,报告上还看不出花过;
- 账本 `record_usage(source=...)` 的默认值是 provider,于是字符估算、缺失用量、回放脚本
  的合成数字全被标成"端点真值",费用口径系统性失真;
- 缓存字段(OpenAI 的 `prompt_tokens_details.cached_tokens`、DeepSeek 的
  `prompt_cache_hit_tokens`/`miss`)是 prompt 的**子集**,把它再加一遍就是双计;
- SDK 隐式重试(max_retries>0)发生在一次 `create()` 里,平台看不见几次物理尝试,
  此前既没记这笔不可观测性,也没法给每次尝试独立 ID。

本模块只定义"计量协议":读数、来源、异常、物理尝试与请求估算。预留与结算是 G03,
这里刻意不碰额度算法(plain_loop 的闸门读数由 M15/M17 的锚点钉住,一字不许回溯改)。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

# 用量来源只有这三种(SPEC §4 契约 2)。默认 unknown:缺失不许冒充真值。
USAGE_PROVIDER = "provider"
USAGE_ESTIMATED = "estimated"
USAGE_UNKNOWN = "unknown"
USAGE_SOURCES = (USAGE_PROVIDER, USAGE_ESTIMATED, USAGE_UNKNOWN)

# 没有 tokenizer 可依时,方法与版本必须随读数一起落盘——否则几个月后没人能解释
# 这批数怎么来的,也没人能否认它"不是硬账单"。
ESTIMATE_METHOD = "chars_div_4"
ESTIMATE_VERSION = "g02.1"

ATTEMPT_OK = "ok"
ATTEMPT_PARSE_FAILED = "parse_failed"
ATTEMPT_TRANSPORT_FAILED = "transport_failed"

# 重试归属:sdk_opaque = 物理尝试次数由 SDK 内部决定,平台只能看见一个上界。
RETRY_PLATFORM = "platform"
RETRY_SDK_OPAQUE = "sdk_opaque"

# 每条消息与每份 schema 的协议框架开销(角色名、分隔符、JSON 包装)。常数而非实测:
# 它只用来把"待发请求"估得更接近真实容量,不参与任何既有闸门的读数。
_MESSAGE_PROTOCOL_TOKENS = 4
_SCHEMA_PROTOCOL_TOKENS = 12

_SECRET_HINTS = ("key", "token", "secret", "authorization", "password", "credential")

# 已知 usage 字段之外的一律只带去敏副本,不参与解释(SPEC:"只解释已知字段")。
_KNOWN_USAGE_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    }
)


def _field(obj: Any, name: str) -> Any:
    """既吃 SDK 对象也吃 dict/SimpleNamespace(测试桩与真实响应同一套解析)。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except (TypeError, ValueError):  # pragma: no cover - 桩对象兜底
            data = None
        if isinstance(data, dict):
            return data
    try:
        return dict(vars(obj))
    except TypeError:
        return {}


def _optional_int(value: Any) -> int | None:
    """非负整数才算合法用量;负数/字符串/None 一律 None(不许"用零充当缺失")。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _redact_extras(raw: Any) -> dict[str, Any]:
    """未知字段的去敏副本:标量留值,长文本与凭据样字段只留字段名。"""
    extras: dict[str, Any] = {}
    for key, value in _mapping(raw).items():
        if key in _KNOWN_USAGE_FIELDS:
            continue
        lowered = key.lower()
        if value is None or isinstance(value, (int, float, bool)):
            # usage 里全是数字:数字不可能是凭据,照原样留(未知字段副本要能复核)
            extras[key] = value
        elif any(hint in lowered for hint in _SECRET_HINTS):
            extras[key] = "<redacted>"
        elif isinstance(value, str):
            extras[key] = value[:64] + ("<truncated>" if len(value) > 64 else "")
        else:
            extras[key] = f"<{type(value).__name__}>"
    return extras


@dataclass
class UsageEnvelope:
    """一次调用的用量与**它的来源**。None 是"不知道",0 是"确实是 0"(§4 契约 2)。

    缓存明细永远是 prompt 的子集,`chargeable_total` 只由 total/明细决定,
    绝不把 cached/uncached 再加一遍进去。
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_source: str = USAGE_UNKNOWN
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_fields: tuple[str, ...] = ()  # 端点声明缓存用的字段名;空 = 该端点无缓存计量
    anomalies: list[str] = field(default_factory=list)
    unknown_attempts: int = 0  # 有多少次物理尝试的用量无从知道(不写 0,不虚报)
    estimate_reason: str = ""
    estimate_method: str = ESTIMATE_METHOD
    estimate_version: str = ESTIMATE_VERSION
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def has_known_usage(self) -> bool:
        return self.chargeable_total is not None

    @property
    def detail_sum(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)

    @property
    def chargeable_total(self) -> int | None:
        """可入账总额:明细和与总额两者都已知时取**较大者**(宁可高估消耗)。"""
        candidates = [v for v in (self.detail_sum, self.total_tokens) if v is not None]
        return max(candidates) if candidates else None

    @property
    def uncached_input_tokens(self) -> int | None:
        """未缓存输入价计价用的那一份:端点直接声明就用声明值,否则由 prompt 扣减。"""
        if self.uncached_tokens is not None:
            return self.uncached_tokens
        if self.prompt_tokens is None or (
            self.cached_tokens is None and self.cache_write_tokens is None
        ):
            return None
        return max(
            0, self.prompt_tokens - int(self.cached_tokens or 0) - int(self.cache_write_tokens or 0)
        )

    # ---------- 构造 ----------

    @classmethod
    def estimated(cls, *, prompt_tokens: int, completion_tokens: int, reason: str) -> UsageEnvelope:
        """本地字符估算:来源如实标 estimated,方法与版本一起留。"""
        return cls(
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            total_tokens=int(prompt_tokens) + int(completion_tokens),
            usage_source=USAGE_ESTIMATED,
            estimate_reason=reason,
        )

    @classmethod
    def unknown(cls, reason: str, *, attempts: int = 1) -> UsageEnvelope:
        """拿不到任何用量:不许写 0,也不许重授额度。"""
        return cls(
            usage_source=USAGE_UNKNOWN,
            unknown_attempts=max(1, int(attempts)),
            anomalies=[f"usage_unavailable:{reason}"],
        )

    @classmethod
    def from_turn_ints(
        cls, *, prompt_tokens: int, completion_tokens: int, total_tokens: int
    ) -> UsageEnvelope:
        """旧式回合(只有三个 int、没带 envelope)的如实换算。

        这三个数是模型层的估算或脚本合成值,没有 provider 背书 ⇒ estimated。
        0 在旧编码里既可能是"没有"也可能是"真是 0",按历史口径当作"没有"。
        """
        prompt = int(prompt_tokens) or None
        completion = int(completion_tokens) or None
        total = int(total_tokens) or None
        env = cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            usage_source=USAGE_ESTIMATED,
            estimate_reason="turn_without_envelope",
        )
        env.check()
        return env

    # ---------- 校验与合成 ----------

    def check(self) -> list[str]:
        """把已知的不一致登记成异常;不静默改数,也不静默放过。"""
        anomalies = self.anomalies
        if (
            self.total_tokens is not None
            and self.detail_sum is not None
            and self.total_tokens != self.detail_sum
        ):
            anomalies.append("total_detail_mismatch")
        if self.prompt_tokens is not None:
            cached = int(self.cached_tokens or 0) + int(self.cache_write_tokens or 0)
            if cached > self.prompt_tokens:
                anomalies.append("cache_exceeds_prompt")
            if (
                self.cached_tokens is not None
                and self.uncached_tokens is not None
                and self.cached_tokens + self.uncached_tokens != self.prompt_tokens
            ):
                anomalies.append("cache_partition_mismatch")
        if self.total_tokens is None and self.detail_sum is None:
            anomalies.append("usage_absent")
        return anomalies

    @classmethod
    def combine(cls, parts: list[UsageEnvelope]) -> UsageEnvelope:
        """一次逻辑调用的多个物理 attempt 合成一份读数。

        已知量相加,未知的次数进 `unknown_attempts`(G03 的预留要拿它当"仍有未知在途");
        来源不一致时整体降为 estimated——混合来源里最弱的一环决定可信度。
        """
        if not parts:
            return cls.unknown("no_attempt")
        known = [p for p in parts if p.has_known_usage]
        if not known:
            merged = cls.unknown(
                "all_attempts_unknown", attempts=sum(p.unknown_attempts or 1 for p in parts)
            )
            merged.extras = {k: v for p in parts for k, v in p.extras.items()}
            merged.anomalies = sorted({a for p in parts for a in p.anomalies})
            return merged
        sources = {p.usage_source for p in known}
        sole_source = next(iter(sources)) if len(sources) == 1 else USAGE_ESTIMATED
        merged = cls(
            prompt_tokens=sum(int(p.prompt_tokens or 0) for p in known)
            if any(p.prompt_tokens is not None for p in known)
            else None,
            completion_tokens=sum(int(p.completion_tokens or 0) for p in known)
            if any(p.completion_tokens is not None for p in known)
            else None,
            total_tokens=sum(int(p.chargeable_total or 0) for p in known),
            usage_source=sole_source,
            cached_tokens=sum(int(p.cached_tokens or 0) for p in known)
            if any(p.cached_tokens is not None for p in known)
            else None,
            cache_write_tokens=sum(int(p.cache_write_tokens or 0) for p in known)
            if any(p.cache_write_tokens is not None for p in known)
            else None,
            uncached_tokens=sum(int(p.uncached_input_tokens or 0) for p in known)
            if any(p.uncached_input_tokens is not None for p in known)
            else None,
            reasoning_tokens=sum(int(p.reasoning_tokens or 0) for p in known)
            if any(p.reasoning_tokens is not None for p in known)
            else None,
            cache_fields=tuple(sorted({f for p in parts for f in p.cache_fields})),
            unknown_attempts=sum(p.unknown_attempts for p in parts),
            estimate_version=ESTIMATE_VERSION,
            extras={k: v for p in parts for k, v in p.extras.items()},
        )
        merged.anomalies = sorted({a for p in parts for a in p.anomalies})
        if len(sources) > 1:
            merged.anomalies = sorted(set(merged.anomalies) | {"mixed_usage_sources"})
            merged.estimate_reason = f"mixed_sources:{sorted(sources)}"
        else:
            # 单一来源要保住"为什么是估算"这句话:合成一次就把 estimate_reason 抹掉,
            # 报告里就只剩下 estimated 这个标签,没人知道它是缺 usage 还是缺明细
            merged.estimate_reason = next(
                (p.estimate_reason for p in parts if p.estimate_reason), ""
            )
        merged.check()
        return merged


@dataclass
class RequestEstimate:
    """一次**待发请求**的容量估算:消息正文 + 工具参数 + 需回传的 reasoning + schema + 协议常数。

    它与额度闸门的读数分开:闸门 `pending` 的算法被 M15/M17 锚点钉住(见 config
    的 token_estimate_factor 注释),这里只补"以前根本没被算进去的那部分容量",
    供 G03 的预留与报告使用。
    """

    content_tokens: int = 0
    tool_argument_tokens: int = 0
    reasoning_tokens: int = 0
    schema_tokens: int = 0
    protocol_tokens: int = 0
    method: str = ESTIMATE_METHOD
    version: str = ESTIMATE_VERSION

    @property
    def total_tokens(self) -> int:
        return (
            self.content_tokens
            + self.tool_argument_tokens
            + self.reasoning_tokens
            + self.schema_tokens
            + self.protocol_tokens
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_tokens": self.content_tokens,
            "tool_argument_tokens": self.tool_argument_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "schema_tokens": self.schema_tokens,
            "protocol_tokens": self.protocol_tokens,
            "total_tokens": self.total_tokens,
            "estimate_method": self.method,
            "estimate_version": self.version,
        }


def estimate_request(
    messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None
) -> RequestEstimate:
    from app.llm.base import estimate_tokens  # 延迟导入:base 只在类型位引用本模块

    estimate = RequestEstimate()
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            estimate.content_tokens += estimate_tokens(content)
        elif content is not None:
            estimate.content_tokens += estimate_tokens(json.dumps(content, default=str))
        if tool_calls := message.get("tool_calls"):
            estimate.tool_argument_tokens += estimate_tokens(repr(tool_calls))
        # 思考端点要求 reasoning_content 原样回传:它占真实容量,漏算就是低估请求
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            estimate.reasoning_tokens += estimate_tokens(reasoning)
        estimate.protocol_tokens += _MESSAGE_PROTOCOL_TOKENS
    if schemas:
        estimate.schema_tokens += estimate_tokens(
            json.dumps(schemas, ensure_ascii=False, default=str)
        )
        estimate.protocol_tokens += _SCHEMA_PROTOCOL_TOKENS
    return estimate


@dataclass
class AttemptRecord:
    """一次**物理**请求:独立 ID、自己的用量、自己的失败状态。"""

    index: int
    attempt_id: str
    status: str
    usage: UsageEnvelope
    retry_owner: str = RETRY_PLATFORM
    sdk_retry_bound: int = 0
    error_class: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "usage_source": self.usage.usage_source,
            "total_tokens": self.usage.chargeable_total,
            "retry_owner": self.retry_owner,
            "sdk_retry_bound": self.sdk_retry_bound,
            "error_class": self.error_class,
        }


def new_attempt_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ModelResponseEnvelope:
    """一次 `complete()` 的全部事实:choices 能不能解释,usage 与 finish_reason 都在。

    SPEC §4 契约 3 的落点:解析失败也必须能查询已知消耗,所以它先于任何业务解释构造。
    """

    usage: UsageEnvelope
    finish_reason: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)
    parse_failures: list[str] = field(default_factory=list)
    thinking_requested: str = ""  # 请求档位名,不含凭据
    reasoning_returned: bool = False  # 只记有无,思维链原文不进报告
    truncated: bool = False
    request_estimate: RequestEstimate | None = None
    # G07:请求名与端点**回报**的名字。别名相同不能证明服务端版本没漂移(§G04),
    # 所以"返回模型"是这次响应的观测事实,进 envelope 而不是进能力声明档案。
    requested_model: str = ""
    served_model: str = ""

    @property
    def model_identity_drift(self) -> bool:
        """端点回报的模型名与我们请求的名字不一致(空返回不算漂移:那是没给)。"""
        return bool(self.served_model and self.served_model != self.requested_model)

    @property
    def usage_source(self) -> str:
        return self.usage.usage_source

    @property
    def physical_attempts(self) -> int:
        return len(self.attempts)

    def as_report_dict(self) -> dict[str, Any]:
        """公开面读数:去敏、无思维链原文、无任意绝对路径。"""
        return {
            "usage_source": self.usage.usage_source,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "chargeable_total": self.usage.chargeable_total,
            "cached_tokens": self.usage.cached_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "uncached_input_tokens": self.usage.uncached_input_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
            "cache_fields": list(self.usage.cache_fields),
            "usage_anomalies": list(self.usage.anomalies),
            "unknown_attempts": self.usage.unknown_attempts,
            "estimate_reason": self.usage.estimate_reason,
            "estimate_version": self.usage.estimate_version,
            "usage_extras": dict(self.usage.extras),
            "finish_reason": self.finish_reason,
            "parse_failures": list(self.parse_failures),
            "truncated": self.truncated,
            "thinking_requested": self.thinking_requested,
            "reasoning_returned": self.reasoning_returned,
            # G07 身份观测:请求名 / 端点回报名 / 两者不一致(名字不是凭据,可进公开面)
            "requested_model": self.requested_model,
            "served_model": self.served_model,
            "model_identity_drift": self.model_identity_drift,
            "physical_attempts": [a.as_dict() for a in self.attempts],
            "request_estimate": self.request_estimate.as_dict() if self.request_estimate else None,
        }


def parse_usage(raw: Any) -> UsageEnvelope:
    """把端点原始 usage 解析成 envelope:只解释已知字段,未知字段带去敏副本。

    两套缓存写法都支持(§G02"可测试字段适配"):标准 `prompt_tokens_details.cached_tokens`
    与 DeepSeek 风格 `prompt_cache_hit_tokens/miss_tokens`;两者同时存在且不一致时
    按**较小**的 cached 入账(少算缓存命中=多算未缓存输入价,宁可高估成本)。
    """
    if raw is None:
        return UsageEnvelope(usage_source=USAGE_UNKNOWN, anomalies=["usage_absent"])

    env = UsageEnvelope(usage_source=USAGE_PROVIDER, extras=_redact_extras(raw))
    env.prompt_tokens = _optional_int(_field(raw, "prompt_tokens"))
    env.completion_tokens = _optional_int(_field(raw, "completion_tokens"))
    env.total_tokens = _optional_int(_field(raw, "total_tokens"))

    details = _field(raw, "prompt_tokens_details")
    standard_cached = (
        _optional_int(_field(details, "cached_tokens")) if details is not None else None
    )
    standard_write = (
        _optional_int(_field(details, "cache_write_tokens")) if details is not None else None
    )
    hit = _optional_int(_field(raw, "prompt_cache_hit_tokens"))
    miss = _optional_int(_field(raw, "prompt_cache_miss_tokens"))

    fields_used: list[str] = []
    if standard_cached is not None:
        fields_used.append("prompt_tokens_details.cached_tokens")
    if standard_write is not None:
        fields_used.append("prompt_tokens_details.cache_write_tokens")
    if hit is not None:
        fields_used.append("prompt_cache_hit_tokens")
    if miss is not None:
        fields_used.append("prompt_cache_miss_tokens")
    candidates = [v for v in (standard_cached, hit) if v is not None]
    env.cached_tokens = min(candidates) if candidates else None
    if standard_cached is not None and hit is not None and standard_cached != hit:
        env.anomalies.append("cache_adapters_disagree")
    env.cache_write_tokens = standard_write
    env.uncached_tokens = miss
    env.cache_fields = tuple(fields_used)

    completion_details = _field(raw, "completion_tokens_details")
    if completion_details is not None:
        env.reasoning_tokens = _optional_int(_field(completion_details, "reasoning_tokens"))
        if env.reasoning_tokens is not None:
            env.cache_fields = (*env.cache_fields, "completion_tokens_details.reasoning_tokens")

    if env.total_tokens is None and env.detail_sum is None:
        # 一个数都没解析出来:这就是"没有 usage",不许挂着 provider 的标签留在那儿
        # (缺陷原句:record_usage 的默认值 provider 把缺失与估算全标成端点真值)。
        env.usage_source = USAGE_UNKNOWN
    env.check()
    return env
