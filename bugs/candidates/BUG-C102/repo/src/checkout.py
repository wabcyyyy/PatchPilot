"""订单结算。"""


def order_total(items):
    """订单总额 = Σ(单价×数量) + 运费;items 为 (名称, 单价, 数量, 单件重量kg)。"""
    subtotal = sum(price * qty for _, price, qty, _ in items)
    return round(subtotal, 2)
