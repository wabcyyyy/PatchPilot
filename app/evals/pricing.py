"""任务成本核算:内置价目表(约值)+ 可选覆盖文件,金额一律走最小货币单位。

价格是 USD / 1M tokens 的"约值",仅供成本参考,不用于计费;
模型名按精确匹配折算,未命中返回 None(不猜测);fake 提供方恒 None。

G03 的两条硬口径(SPEC §5):
- **累计与判定用 micro-USD 整数**(1 USD = 1_000_000)。浮点相加会让"花了多少"取决于
  相加顺序,而现金上限是会被拿去停手、拿去报账的数;`estimate_cost` 的 float 接口只保留
  作历史读数口径(report.json 的 `cost_usd`),不参与累计判定。
- **"端点 usage 乘价表"仍然只是估计账单**。它标 `provider_usage_priced`,不等于
  `settled_bill`(与 provider 账单对账后的金额)。本仓库没有任何账单来源,所以
  settled_bill 只能由外部显式入账,不会被算出来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm.usage import USAGE_ESTIMATED, USAGE_PROVIDER, UsageEnvelope

log = logging.getLogger(__name__)

# (输入, 输出) USD / 1M tokens,约值:官方公布价四舍五入,报告须注明"约值"
# deepseek-flash(V4.1 Flash)官方价分 peak/off-peak(对半),此处按 peak 高档折算,
# 保证成本记录宁可高估;off-peak 跑批实际成本约为表值一半
PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-flash": (0.30, 1.20),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

# 价目表版本:条目变了必须升它。它与 price_source 一起进运行身份(G04 的登记面),
# 否则"换了价目"会被读成"同一个配置跑了两批"。
PRICING_VERSION = "2026-10-builtin-1"
CURRENCY = "USD"
MICRO_USD_PER_USD = Decimal(1_000_000)
_PER_MILLION = Decimal(1_000_000)

# 费用来源标签(§4 契约 2 / §7 G03):三级分明,不许互相冒充
COST_LOCAL_ESTIMATE = "local_estimate"  # 本地字符估算的 token × 价目
COST_PROVIDER_USAGE = "provider_usage_priced"  # 端点 usage × 价目:仍是估,不是账单
COST_SETTLED_BILL = "settled_bill"  # 对账后的实际账单(本仓库无来源,只能外部传入)
COST_BASES = (COST_LOCAL_ESTIMATE, COST_PROVIDER_USAGE, COST_SETTLED_BILL)


def _overrides_raw() -> dict[str, Any]:
    """覆盖文件的原始对象;读不动/坏 JSON 一律回空(与 N-14 的容错口径一致)。"""
    path = get_settings().price_overrides
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("price_overrides %s ignored: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("price_overrides %s ignored: top-level value is not an object", path)
        return {}
    return raw


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def price_card_for(model: str) -> PriceCard | None:
    """模型 → 冻结价卡;未收录返回 None(不猜测价格),fake 提供方恒 None。

    条目两种写法都吃:`[输入, 输出]`(历史形状,可给四项补缓存档)或
    `{"input":…, "output":…, "cached_input":…, "cache_write":…}`。坏条目逐条跳过并告警:
    价目表不能把一个已完成的任务炸成 NEEDS_REVIEW(N-14 的原教训)。
    """
    if not model or model.startswith("fake"):
        return None
    builtin = PRICES.get(model)
    entry = _overrides_raw().get(model)
    if entry is None and builtin is None:
        return None
    input_usd = output_usd = cached_usd = write_usd = None
    source = "builtin"
    if isinstance(entry, dict):
        input_usd = _decimal(entry.get("input"))
        output_usd = _decimal(entry.get("output"))
        cached_usd = _decimal(entry.get("cached_input"))
        write_usd = _decimal(entry.get("cache_write"))
        source = "overrides"
    elif isinstance(entry, (list, tuple)):
        parts = list(entry)
        if len(parts) >= 2:
            input_usd, output_usd = _decimal(parts[0]), _decimal(parts[1])
            if len(parts) >= 4:
                cached_usd, write_usd = _decimal(parts[2]), _decimal(parts[3])
            source = "overrides"
        else:
            log.warning("price_overrides: bad entry for %r ignored", model)
    elif entry is not None:
        log.warning("price_overrides: bad entry for %r ignored", model)

    if input_usd is None or output_usd is None:
        if builtin is None:
            log.warning("price_overrides: entry for %r has no usable price; ignored", model)
            return None
        input_usd = Decimal(str(builtin[0]))
        output_usd = Decimal(str(builtin[1]))
    return PriceCard(
        model=model,
        input_usd_per_mtok=input_usd,
        output_usd_per_mtok=output_usd,
        cached_input_usd_per_mtok=cached_usd,
        cache_write_usd_per_mtok=write_usd,
        source=source,
    )


@dataclass(frozen=True)
class PriceCard:
    """一个模型的冻结价目:单价 + 币种 + 版本 + 来源。"""

    model: str
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal | None = None
    cache_write_usd_per_mtok: Decimal | None = None
    currency: str = CURRENCY
    version: str = PRICING_VERSION
    source: str = "builtin"  # builtin | overrides

    @property
    def declares_cache_pricing(self) -> bool:
        return self.cached_input_usd_per_mtok is not None or (
            self.cache_write_usd_per_mtok is not None
        )

    def cost_micro_usd(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> int:
        """输入按"未缓存 + 缓存读 + 缓存写"分档计价,总额向上取整(保守)。

        cached/cache_write 是 prompt 的**子集**,只从 prompt 里切出来单独计价,
        绝不额外加进总量(§4 契约 2)。价卡没声明缓存价时那部分按未缓存价算——
        宁可给一个偏贵的上限,也不虚构一个看起来更便宜的折扣。
        """
        prompt = max(int(prompt_tokens), 0)
        cached = min(max(int(cached_tokens or 0), 0), prompt)
        wrote = min(max(int(cache_write_tokens or 0), 0), prompt - cached)
        uncached = prompt - cached - wrote
        cached_price = (
            self.cached_input_usd_per_mtok
            if self.cached_input_usd_per_mtok is not None
            else self.input_usd_per_mtok
        )
        write_price = (
            self.cache_write_usd_per_mtok
            if self.cache_write_usd_per_mtok is not None
            else self.input_usd_per_mtok
        )
        total = (
            Decimal(uncached) * self.input_usd_per_mtok
            + Decimal(cached) * cached_price
            + Decimal(wrote) * write_price
            + Decimal(max(int(completion_tokens), 0)) * self.output_usd_per_mtok
        )
        micro = total * MICRO_USD_PER_USD / _PER_MILLION
        return int(micro.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class CostRecord:
    """一笔费用的事实:金额(micro-USD)+ 它凭什么被算出来。"""

    micro_usd: int
    currency: str
    price_version: str
    price_source: str
    basis: str  # COST_LOCAL_ESTIMATE | COST_PROVIDER_USAGE | COST_SETTLED_BILL
    cache_pricing: str = "declared"  # declared | undiscounted(价卡未声明缓存档)

    @property
    def usd(self) -> float:
        return self.micro_usd / float(MICRO_USD_PER_USD)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_micro_usd": self.micro_usd,
            "currency": self.currency,
            "price_version": self.price_version,
            "price_source": self.price_source,
            "cost_basis": self.basis,
            "cache_pricing": self.cache_pricing,
        }


def cost_of_usage(
    model: str, usage: UsageEnvelope, *, basis: str | None = None
) -> CostRecord | None:
    """按用量折价。token 是估的,费用就必须是估的:来源标签跟着 usage 走。"""
    if basis is not None and basis not in COST_BASES:
        raise ValueError(f"unknown cost basis: {basis!r}(one of {COST_BASES})")
    card = price_card_for(model)
    if card is None:
        return None
    label = basis or (
        COST_PROVIDER_USAGE if usage.usage_source == USAGE_PROVIDER else COST_LOCAL_ESTIMATE
    )
    return CostRecord(
        micro_usd=card.cost_micro_usd(
            prompt_tokens=int(usage.prompt_tokens or 0),
            completion_tokens=int(usage.completion_tokens or 0),
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        ),
        currency=card.currency,
        price_version=card.version,
        price_source=card.source,
        basis=label,
        cache_pricing="declared" if card.declares_cache_pricing else "undiscounted",
    )


def settled_cost(model: str, micro_usd: int, *, currency: str = CURRENCY) -> CostRecord:
    """外部账单入账的唯一入口:显式传入才标 settled_bill。"""
    card = price_card_for(model)
    return CostRecord(
        micro_usd=max(int(micro_usd), 0),
        currency=currency,
        price_version=card.version if card else "external",
        price_source="external_bill",
        basis=COST_SETTLED_BILL,
    )


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """折算一次任务的美元成本;未收录模型返回 None,不猜测价格。

    历史 float 接口(报告与既有批次在读);累计判定请用 `cost_of_usage`。
    """
    usage = UsageEnvelope(
        prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens),
        usage_source=USAGE_ESTIMATED,
    )
    record = cost_of_usage(model, usage)
    return None if record is None else record.usd
