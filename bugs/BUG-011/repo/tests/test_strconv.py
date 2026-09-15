from src.strconv import parse_int


def test_valid_number():
    assert parse_int("42") == 42


def test_invalid_returns_default():
    assert parse_int("abc") is None


def test_custom_default():
    assert parse_int("x", default=-1) == -1


def test_whitespace_number():
    assert parse_int(" 7 ") == 7
