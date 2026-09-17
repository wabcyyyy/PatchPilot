"""购物车。"""


class Cart:
    """购物车;items 记录商品 → 数量。"""

    items = {}

    def add(self, product, quantity=1):
        self.items[product] = self.items.get(product, 0) + quantity
        return self.items

    def total_quantity(self):
        return sum(self.items.values())
