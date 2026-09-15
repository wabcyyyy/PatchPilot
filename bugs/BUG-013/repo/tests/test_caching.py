from src.caching import get_or_set


def test_computes_and_caches_value():
    cache = {}
    assert get_or_set(cache, "k", lambda: 42) == 42
    assert cache["k"] == 42


def test_fn_called_once():
    calls = []

    def factory():
        calls.append(1)
        return "v"

    cache = {}
    get_or_set(cache, "k", factory)
    get_or_set(cache, "k", factory)
    assert len(calls) == 1


def test_existing_value_returned():
    assert get_or_set({"k": 7}, "k", lambda: 1) == 7
