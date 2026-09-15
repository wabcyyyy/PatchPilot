from src.dateparse import parse_date


def test_parse_iso_format():
    assert parse_date("2026-09-15").isoformat() == "2026-09-15"


def test_slash_format():
    assert parse_date("2026/09/15").isoformat() == "2026-09-15"


def test_empty_string_returns_none():
    # 基线故意失败:当前实现抛 ValueError 而不是返回 None
    assert parse_date("") is None
