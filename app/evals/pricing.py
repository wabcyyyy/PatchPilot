"""任务成本核算:内置价目表(约值)+ 可选覆盖文件。

价格是 USD / 1M tokens 的"约值",仅供成本参考,不用于计费;
模型名按精确匹配折算,未命中返回 None(不猜测);fake 提供方恒 None。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

# (输入, 输出) USD / 1M tokens,约值:官方公布价四舍五入,报告须注明"约值"
PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def _load_prices() -> dict[str, tuple[float, float]]:
    """内置价目 + 可选覆盖文件(Settings.price_overrides,优先级更高)。

    N-14 整改:坏条目逐条跳过并告警,不再让畸形覆盖文件炸掉 estimate_cost——
    该函数在任务收尾的 finally 里调用,一旦抛出,report.json 不落盘,
    已完成的任务会被静默吞成 NEEDS_REVIEW。
    """
    prices = dict(PRICES)
    path = get_settings().price_overrides
    if not path:
        return prices
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("price_overrides %s ignored: %s", path, exc)
        return prices
    if not isinstance(raw, dict):
        log.warning("price_overrides %s ignored: top-level value is not an object", path)
        return prices
    for model, pair in raw.items():
        try:
            prices[str(model)] = (float(pair[0]), float(pair[1]))
        except (IndexError, KeyError, TypeError, ValueError):
            log.warning("price_overrides %s: bad entry for %r ignored", path, model)
    return prices


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """折算一次任务的美元成本;未收录模型返回 None,不猜测价格。"""
    if model.startswith("fake"):
        return None
    price = _load_prices().get(model)
    if price is None:
        return None
    prompt_usd, completion_usd = price
    return prompt_tokens / 1_000_000 * prompt_usd + completion_tokens / 1_000_000 * completion_usd
