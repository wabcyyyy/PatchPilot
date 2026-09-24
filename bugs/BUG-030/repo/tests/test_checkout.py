from src.checkout import order_total
from src.fees import shipping_for


def test_order_includes_shipping():
    assert order_total([("desk", 50.0, 1, 12.0)]) == 70.0


def test_heavy_multi_quantity_order():
    # 2 × 6kg = 12kg,运费封顶 20;小计 60
    assert order_total([("lamp", 30.0, 2, 6.0)]) == 80.0


def test_shipping_capped_for_heavy():
    # 8 + 1.5 × 12 = 26,封顶 20
    assert shipping_for(12.0) == 20.0


def test_light_shipping_uncapped():
    assert shipping_for(1.0) == 9.5


def test_zero_weight_shipping():
    assert shipping_for(0.0) == 8.0
