import pytest
from src.dateparse import parse_date


def test_iso_format():
    assert parse_date("2026-01-31").isoformat() == "2026-01-31"


def test_slash_format():
    assert parse_date("2026/01/31").isoformat() == "2026-01-31"


def test_invalid_format_raises():
    with pytest.raises(ValueError):
        parse_date("31-01-2026")


def test_none_returns_none():
    assert parse_date(None) is None


def test_empty_string_returns_none():
    assert parse_date("") is None


def test_whitespace_returns_none():
    assert parse_date("   ") is None
