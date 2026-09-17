from src.carts import Cart


def test_add_accumulates_in_one_cart():
    cart = Cart()
    cart.add("pear", 2)
    cart.add("pear", 1)
    assert cart.items["pear"] == 3


def test_items_lookup_by_product():
    cart = Cart()
    cart.add("fig", 4)
    assert cart.items["fig"] == 4


def test_carts_are_independent():
    first = Cart()
    second = Cart()
    first.add("melon", 2)
    second.add("peach", 1)
    assert first.total_quantity() == 2
    assert second.total_quantity() == 1
