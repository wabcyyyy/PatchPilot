from src.seq import first_or


def test_returns_first():
    assert first_or([3, 1], default=0) == 3


def test_empty_returns_default():
    assert first_or([], default="none") == "none"


def test_default_is_none():
    assert first_or([]) is None
