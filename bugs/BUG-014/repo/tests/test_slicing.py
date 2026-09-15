import pytest
from src.slicing import chunk


def test_even_split():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_remainder_chunk():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_invalid_n_raises():
    with pytest.raises(ValueError):
        chunk([1], 0)


def test_empty_input():
    assert chunk([], 3) == []
