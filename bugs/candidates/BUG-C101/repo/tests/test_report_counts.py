from src.report import render_counts


def test_duplicated_names_are_summed():
    assert render_counts([("b", 2), ("a", 1), ("b", 3)]) == "a: 1\nb: 5"


def test_unique_names_kept():
    assert render_counts([("api", 2), ("web", 1)]) == "api: 2\nweb: 1"


def test_empty_input():
    assert render_counts([]) == ""


def test_zero_count_is_kept():
    assert render_counts([("cache", 0)]) == "cache: 0"
