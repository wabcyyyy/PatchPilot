from src.notes import add_note


def test_explicit_list_is_mutated():
    mine = ["a"]
    result = add_note("b", mine)
    assert result == ["a", "b"]
    assert mine == ["a", "b"]


def test_default_calls_are_isolated():
    first = add_note("first")
    assert first == ["first"]
    second = add_note("second")
    assert second == ["second"]
    assert first == ["first"]


def test_explicit_empty_list():
    assert add_note("x", []) == ["x"]
