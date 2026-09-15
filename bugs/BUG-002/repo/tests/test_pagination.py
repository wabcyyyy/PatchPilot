import pytest
from src.pagination import page_slice

DATA = list(range(10))


def test_first_page_full():
    assert page_slice(DATA, 1, 3) == [0, 1, 2]


def test_last_page_partial():
    assert page_slice(DATA, 4, 3) == [9]


def test_invalid_page_raises():
    with pytest.raises(ValueError):
        page_slice(DATA, 0, 3)


def test_page_beyond_end_is_empty():
    assert page_slice(DATA, 99, 3) == []
