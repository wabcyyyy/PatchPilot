from src.calendar_ops import is_leap


def test_regular_leap_year():
    assert is_leap(2024) is True


def test_century_non_leap():
    assert is_leap(1900) is False


def test_400_year_is_leap():
    assert is_leap(2000) is True


def test_common_year():
    assert is_leap(2023) is False
