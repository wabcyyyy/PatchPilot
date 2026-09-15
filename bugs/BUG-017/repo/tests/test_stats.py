import pytest
from src.stats import average


def test_fractional_average():
    assert average([1, 2]) == 1.5


def test_integer_average():
    assert average([2, 4]) == 3.0


def test_empty_raises():
    with pytest.raises(ValueError):
        average([])
