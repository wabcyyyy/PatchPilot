"""购物车结算。"""

from src.pricing import unit_price


def cart_total(items):
    """合计:每件商品按数量计价,单价内部应用折扣。"""
    return round(sum(unit_price(name) * qty for name, qty in items), 2)
