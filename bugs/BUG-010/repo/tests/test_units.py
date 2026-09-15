from src.units import to_percent


def test_normal_value():
    assert to_percent(0.123) == 12.3


def test_above_range_clamped():
    assert to_percent(1.5) == 100.0


def test_negative_clamped():
    assert to_percent(-0.2) == 0.0


def test_boundary_one():
    assert to_percent(1.0) == 100.0
