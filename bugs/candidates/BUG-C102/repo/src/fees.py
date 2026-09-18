"""运费规则。"""

BASE_SHIPPING = 8.0
PER_KG = 1.5
HEAVY_WEIGHT_KG = 10.0
HEAVY_SHIPPING_CAP = 20.0


def shipping_for(weight_kg):
    """运费 = 基础价 + 按公斤计费;重量超过 HEAVY_WEIGHT_KG 时封顶 HEAVY_SHIPPING_CAP。"""
    return round(BASE_SHIPPING + PER_KG * weight_kg, 2)
