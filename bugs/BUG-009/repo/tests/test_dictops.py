from src.dictops import merge


def test_extra_overrides_base():
    assert merge({"a": 1, "b": 2}, {"b": 9}) == {"a": 1, "b": 9}


def test_inputs_not_mutated():
    base = {"a": 1}
    merge(base, {"b": 2})
    assert base == {"a": 1}


def test_empty_extra():
    assert merge({"a": 1}, {}) == {"a": 1}
