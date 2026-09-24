"""库存查询。"""

STOCK = {"pen": 3, "book": 0, "lamp": 10, "desk": 5}


def quantity(sku):
    """当前库存量;未知商品返回 None。"""
    return STOCK.get(sku)
