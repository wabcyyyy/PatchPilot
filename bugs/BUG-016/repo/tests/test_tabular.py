from src.tabular import index_of


def test_found_returns_index():
    rows = [{"id": "a"}, {"id": "b"}]
    assert index_of(rows, "id", "b") == 1


def test_missing_column_is_skipped():
    rows = [{"other": 1}, {"id": "x"}]
    assert index_of(rows, "id", "x") == 1


def test_not_found_returns_minus_one():
    assert index_of([{"id": "a"}], "id", "z") == -1


def test_empty_rows():
    assert index_of([], "id", "a") == -1
