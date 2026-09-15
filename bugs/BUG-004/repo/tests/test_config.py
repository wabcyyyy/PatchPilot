from src.config import DEFAULTS, lookup_setting


def test_store_value_wins():
    assert lookup_setting({"timeout": 5}, "timeout") == 5


def test_fallback_to_default():
    assert lookup_setting({}, "retries") == DEFAULTS["retries"]


def test_unknown_key_returns_none():
    assert lookup_setting({}, "nope") is None


def test_partial_store():
    assert lookup_setting({"retries": 9}, "timeout") == DEFAULTS["timeout"]
