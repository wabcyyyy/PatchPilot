from src.math_ops import safe_divide


def test_normal_division():
    assert safe_divide(6, 3) == 2.0


def test_zero_divisor_returns_none():
    assert safe_divide(1, 0) is None


def test_negative_divisor():
    assert safe_divide(6, -3) == -2.0


def test_float_result():
    assert safe_divide(1, 4) == 0.25
