"""库存预警规则。"""

LOW_STOCK_THRESHOLD = 5


def is_low(qty):
    """库存量是否达到低库存预警线(qty <= LOW_STOCK_THRESHOLD,含恰好等于)。"""
    return qty < LOW_STOCK_THRESHOLD
