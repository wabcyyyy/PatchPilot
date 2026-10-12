"""模型能力档案(G07):能力**只能显式声明,不由模型名推断**。

缺陷出处(SPEC §5 / 验收矩阵 T12):配置里只有一个 `model_context_tokens` 数字,它同时
被当成"端点能装多少"和"我们打算装多少"来用,而没有任何地方回答**这个数字是从哪来的**。
名字里带 128k/1M 的模型名一旦被当作依据,平台就会宣称一个从未验证过的窗口,超长保护区
反复压缩到耗尽预算也不报错。本模块把"声明"与"假设"分开:

- 有来源的容量(`operator_config` / `provider_docs` / `probed`)才叫**已验证声明**;
- 没有来源就是 `unverified`,此时容量取保守上界,且**申报比保守上界更大一律拒绝**
  (`unverified` 不是"先按 128k 跑跑看");
- 没声明的能力(工具/思考/缓存明细)留 `None` = 不知道,并进 `assumed` 清单一起落盘,
  报告里看得见的必须是"哪几项是我们替端点假设的"。

这里不产生任何估算口径:估算方法与版本沿用 `app.llm.usage`,价目身份沿用
`app.evals.pricing`,端点去敏身份沿用 `app.evals.provenance.endpoint_identity`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.errors import TaskError
from app.llm.usage import ESTIMATE_METHOD, ESTIMATE_VERSION

CAPABILITY_SCHEMA_VERSION = 1

SOURCE_OPERATOR_CONFIG = "operator_config"
SOURCE_PROVIDER_DOCS = "provider_docs"
SOURCE_PROBED = "probed"
SOURCE_UNVERIFIED = "unverified"
CAPACITY_SOURCES = (SOURCE_OPERATOR_CONFIG, SOURCE_PROVIDER_DOCS, SOURCE_PROBED, SOURCE_UNVERIFIED)

# 无来源时的保守上界:与 legacy_v1 已在用的软窗口同级(16k),而不是"当今主流模型的
# 常见标称值"。宁可让 repair_v2 在这个档上被夹小并如实报错,也不替未验证的端点背书。
UNVERIFIED_CAPACITY_TOKENS = 16_000

# `model_capabilities` 的合法项:name=yes|no。写错一律拒绝,坏字符串静默当成"没声明"
# 会让一个本来想说"这端点不会调工具"的配置长成"假设它会"。
_CAPABILITY_FIELDS = {
    "tools": "supports_tools",
    "reasoning": "supports_reasoning",
    "cached_usage": "reports_cached_usage",
}


def parse_capabilities(raw: str) -> dict[str, bool]:
    """解析 `name=yes|no` 逗号清单;空串 = 什么都没声明。"""
    declared: dict[str, bool] = {}
    for item in (part.strip() for part in (raw or "").split(",")):
        if not item:
            continue
        name, _, value = item.partition("=")
        name = name.strip().lower()
        value = value.strip().lower()
        if name not in _CAPABILITY_FIELDS or value not in {"yes", "no"}:
            legal = ", ".join(f"{name}=yes|no" for name in _CAPABILITY_FIELDS)
            raise TaskError(
                f"model_capabilities entry {item!r} is not one of: {legal}"
                "(能力项拼错不许被当作未声明)"
            )
        declared[_CAPABILITY_FIELDS[name]] = value == "yes"
    return declared


@dataclass(frozen=True)
class ModelProfile:
    """一个"端点 + 模型"的能力声明。它描述边界,不描述愿望。"""

    requested_model: str = ""
    capacity_tokens: int = UNVERIFIED_CAPACITY_TOKENS
    capacity_source: str = SOURCE_UNVERIFIED
    output_ceiling_tokens: int = 0  # 端点声明的 completion 上限;0 = 未声明
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    reports_cached_usage: bool | None = None
    assumed: tuple[str, ...] = ()  # 未声明、由保守档代答的字段名(要落盘)
    endpoint: Mapping[str, Any] = field(default_factory=dict)  # 去敏身份,永不含 key
    estimate_method: str = ESTIMATE_METHOD
    estimate_version: str = ESTIMATE_VERSION
    price_present: bool = False
    price_version: str = ""
    price_source: str = ""

    def __post_init__(self) -> None:
        if self.capacity_source not in CAPACITY_SOURCES:
            raise TaskError(
                f"unknown capacity source {self.capacity_source!r}; must be one of"
                f" {list(CAPACITY_SOURCES)}"
            )
        if self.capacity_tokens < 0:
            raise TaskError(f"capacity_tokens must be >= 0, got {self.capacity_tokens}")
        if self.capacity_source != SOURCE_UNVERIFIED and self.capacity_tokens <= 0:
            # 有来源却没数字 = 没有可比较的边界,比"没来源"更糟:它看起来像已验证
            raise TaskError(
                f"capacity_source={self.capacity_source} requires a positive"
                " model_context_tokens(声明了来源却没给容量,报告里就是个假声明)"
            )
        if self.capacity_source == SOURCE_UNVERIFIED and self.capacity_tokens > (
            UNVERIFIED_CAPACITY_TOKENS
        ):
            raise TaskError(
                f"unverified capacity claim {self.capacity_tokens} exceeds the conservative"
                f" bound {UNVERIFIED_CAPACITY_TOKENS};给出 operator_config/provider_docs/probed"
                " 来源才允许申报更大窗口(不宣称未验证端点支持大窗口)"
            )

    # ---------- 声明的性质 ----------

    @property
    def capacity_verified(self) -> bool:
        """容量是否来自一个有来源的声明(不是我们自己保守填的)。"""
        return self.capacity_source != SOURCE_UNVERIFIED

    @property
    def conservative(self) -> bool:
        return not self.capacity_verified

    # ---------- 与请求容量比较 ----------

    def capacity_ceiling(self, *, output_reserve: int = 0, margin: int = 0) -> int:
        """单请求**输入**能装的上限:硬容量 − 输出预留 − 容量余量。"""
        return max(
            0,
            self.capacity_tokens - max(int(output_reserve), 0) - max(int(margin), 0),
        )

    def adopt_window(
        self, target_tokens: int, *, output_reserve: int = 0, margin: int = 0
    ) -> tuple[int, bool]:
        """能力允许才采用目标窗口。返回 (实际采用值, 是否被容量夹过)。

        这里只夹小,绝不放大:目标 64k 落在未验证的 16k 档上就变成 ~10.8k,并由调用方
        把 `clamped` 记进读数——"profile 写着 64k 而端点只装得下这些"必须是看得见的。
        """
        ceiling = self.capacity_ceiling(output_reserve=output_reserve, margin=margin)
        target = int(target_tokens)
        if target <= 0:  # 0 = 关闭软压缩,由调用方自行满足硬容量
            return target, False
        if ceiling >= target:
            return target, False
        return ceiling, True

    def assert_usable_for_repair(self) -> None:
        """修复 Agent 的最低能力要求:会调工具。

        只有**显式声明为不会**才拒绝(未声明仍是假设,由 `assumed` 说明)。没有工具调用
        就没有检索、没有补丁、没有测试反馈——整条流水线无从执行,不该让它开跑。
        """
        if self.supports_tools is False:
            raise TaskError(
                f"model '{self.requested_model}' declares tools=no;修复 Agent 需要工具调用,"
                "此端点不可用于本任务(不静默退化成只能聊文本的运行)"
            )

    # ---------- 读数 ----------

    def as_dict(self) -> dict[str, Any]:
        """公开面读数:去敏端点身份 + 每一项声明与假设。"""
        return {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "requested_model": self.requested_model,
            "capacity_tokens": self.capacity_tokens,
            "capacity_source": self.capacity_source,
            "capacity_verified": self.capacity_verified,
            "output_ceiling_tokens": self.output_ceiling_tokens,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "reports_cached_usage": self.reports_cached_usage,
            "assumed": list(self.assumed),
            "endpoint": dict(self.endpoint),
            "estimate_method": self.estimate_method,
            "estimate_version": self.estimate_version,
            "price_present": self.price_present,
            "price_version": self.price_version,
            "price_source": self.price_source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelProfile:
        """由 as_dict() 读数重建(恢复侧按受理时冻结的声明复原,不回读当前配置)。

        只吃 as_dict() 的键;capacity_verified 是派生值不收。缺键落数据类默认,
        声明本身非法(来源/容量矛盾)仍由 __post_init__ 当场拒绝——重建不放松校验。
        """
        assumed = data.get("assumed") or []
        return cls(
            requested_model=str(data.get("requested_model", "") or ""),
            capacity_tokens=int(data.get("capacity_tokens", 0) or 0),
            capacity_source=str(
                data.get("capacity_source", SOURCE_UNVERIFIED) or SOURCE_UNVERIFIED
            ),
            output_ceiling_tokens=int(data.get("output_ceiling_tokens", 0) or 0),
            supports_tools=data.get("supports_tools"),
            supports_reasoning=data.get("supports_reasoning"),
            reports_cached_usage=data.get("reports_cached_usage"),
            assumed=tuple(str(item) for item in assumed),
            endpoint=dict(data.get("endpoint") or {}),
            estimate_method=str(data.get("estimate_method", ESTIMATE_METHOD) or ESTIMATE_METHOD),
            estimate_version=str(
                data.get("estimate_version", ESTIMATE_VERSION) or ESTIMATE_VERSION
            ),
            price_present=bool(data.get("price_present", False)),
            price_version=str(data.get("price_version", "") or ""),
            price_source=str(data.get("price_source", "") or ""),
        )


def profile_from_settings(settings: Any) -> ModelProfile:
    """运维显式声明 → 档案。没有声明的一律进 `assumed`,不从 `llm_model` 的名字里猜。"""
    from app.evals.pricing import price_card_for
    from app.evals.provenance import endpoint_identity

    declared_capacity = int(getattr(settings, "model_context_tokens", 0) or 0)
    source = str(getattr(settings, "model_capacity_source", "") or "").strip() or SOURCE_UNVERIFIED
    if source not in CAPACITY_SOURCES:
        raise TaskError(
            f"model_capacity_source={source!r} must be one of {list(CAPACITY_SOURCES)}"
            "(空值按 unverified 处理:保守档,不宣称支持大窗口)"
        )
    capabilities = parse_capabilities(str(getattr(settings, "model_capabilities", "") or ""))
    ceiling = int(getattr(settings, "model_declared_output_ceiling", 0) or 0)

    capacity = declared_capacity
    assumed: list[str] = []
    if source == SOURCE_UNVERIFIED:
        if capacity <= 0:
            capacity = UNVERIFIED_CAPACITY_TOKENS
        # 比保守上界更大的申报原样交给 __post_init__ 拒绝:在这里悄悄改小等于替一个
        # 没提供来源的端点背书。
        assumed.append("capacity_tokens")  # 数字有了但没有来源,仍然是假设
    elif declared_capacity <= 0:
        raise TaskError(
            f"model_capacity_source={source} 需要正的 PATCHPILOT_MODEL_CONTEXT_TOKENS;"
            "有来源却没数字就是一条假声明(不退回保守档,那会把来源借给没声明的容量)"
        )
    if ceiling <= 0:
        assumed.append("output_ceiling_tokens")
    assumed.extend(
        name
        for name in ("supports_tools", "supports_reasoning", "reports_cached_usage")
        if name not in capabilities
    )

    card = price_card_for(str(getattr(settings, "llm_model", "") or ""))
    return ModelProfile(
        requested_model=str(getattr(settings, "llm_model", "") or ""),
        capacity_tokens=capacity,
        capacity_source=source,
        output_ceiling_tokens=ceiling,
        supports_tools=capabilities.get("supports_tools"),
        supports_reasoning=capabilities.get("supports_reasoning"),
        reports_cached_usage=capabilities.get("reports_cached_usage"),
        assumed=tuple(assumed),
        endpoint=endpoint_identity(str(getattr(settings, "llm_base_url", "") or "")),
        price_present=card is not None,
        price_version=card.version if card is not None else "",
        price_source=card.source if card is not None else "",
    )
