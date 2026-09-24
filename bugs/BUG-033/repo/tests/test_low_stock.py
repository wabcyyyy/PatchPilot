from src.notify import low_stock_skus
from src.rules import is_low


def test_out_of_stock_is_reported():
    assert low_stock_skus(["pen", "book"]) == ["pen", "book"]


def test_threshold_boundary_is_low():
    # desk 恰好 5 件,达到预警线
    assert low_stock_skus(["desk"]) == ["desk"]


def test_is_low_at_threshold():
    assert is_low(5) is True


def test_is_low_below_threshold():
    assert is_low(4) is True


def test_overstock_not_reported():
    assert low_stock_skus(["lamp"]) == []


def test_unknown_sku_skipped():
    assert low_stock_skus(["ghost"]) == []


def test_empty_catalog():
    assert low_stock_skus([]) == []
