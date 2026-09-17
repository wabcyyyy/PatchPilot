from src.accounting import total_cents


def test_whole_yuan_prices():
    assert total_cents([1, 2]) == 300


def test_half_values():
    assert total_cents([2.5, 1.25]) == 375


def test_empty_list():
    assert total_cents([]) == 0


def test_single_price_converts_exactly():
    assert total_cents([0.29]) == 29


def test_sum_below_boundary_rounds_up():
    assert total_cents([0.01, 0.06]) == 7
