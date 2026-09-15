from src.collections_ops import concat


def test_two_lists():
    assert concat([1], [2, 3]) == [1, 2, 3]


def test_list_and_tuple():
    assert concat([1], (2, 3)) == [1, 2, 3]


def test_strings_sequence():
    assert concat("ab", "cd") == ["a", "b", "c", "d"]


def test_empty_inputs():
    assert concat([], ()) == []
