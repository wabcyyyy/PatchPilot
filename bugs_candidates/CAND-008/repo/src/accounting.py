"""记账:结算购物清单的总价(以分为单位的整数)。"""


def total_cents(unit_prices):
    """把以"元"计的单价列表累加,返回以"分"为单位的整数总价。"""
    return int(sum(unit_prices) * 100)
