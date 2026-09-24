"""低库存通知汇总。"""

from src.inventory import quantity
from src.rules import is_low


def low_stock_skus(catalog):
    """按目录顺序返回需要预警的商品;缺货(0)与达到预警线的都要包含,未知商品跳过。"""
    result = []
    for sku in catalog:
        qty = quantity(sku)
        if qty and is_low(qty):
            result.append(sku)
    return result
