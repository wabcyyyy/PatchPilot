"""定价规则。"""

PRICE_LIST = {"pen": 5.0, "book": 60.0, "lamp": 120.0, "desk": 100.0}
BULK_THRESHOLD = 100
BULK_DISCOUNT = 0.9


def unit_price(name):
    """单价:原价超过 BULK_THRESHOLD 打 BULK_DISCOUNT 折。"""
    price = PRICE_LIST[name]
    if price > BULK_THRESHOLD:
        return price * BULK_DISCOUNT
    return price
