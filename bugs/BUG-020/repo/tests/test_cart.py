from src.cart import cart_total


def test_no_discount_items():
    assert cart_total([("pen", 2)]) == 10.0


def test_discount_applies_at_threshold():
    # desk 原价恰好 100,满 100 打 9 折 = 90
    assert cart_total([("desk", 1)]) == 90.0


def test_mixed_items():
    # book 60 不打折,lamp 120 打 9 折 108
    assert cart_total([("book", 1), ("lamp", 1)]) == 168.0
