from src.dedup import unique


def test_deduplicates():
    assert sorted(unique(["a", "b", "a"])) == ["a", "b"]


def test_no_duplicates_kept():
    out = unique(["a", "b", "a", "c"])
    assert sorted(out) == ["a", "b", "c"]


def test_empty_input():
    assert unique([]) == []


def test_preserves_first_seen_order():
    assert unique([2, 1, 3, 1, 2]) == [2, 1, 3]
