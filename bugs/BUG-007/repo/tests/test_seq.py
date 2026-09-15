import pytest
from src.seq import middle


def test_odd_length():
    assert middle([1, 2, 3]) == 2


def test_even_length_takes_earlier():
    assert middle([1, 2, 3, 4]) == 2


def test_single_element():
    assert middle([9]) == 9


def test_empty_raises():
    with pytest.raises(ValueError):
        middle([])
