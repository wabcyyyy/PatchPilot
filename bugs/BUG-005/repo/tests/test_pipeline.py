from src.pipeline import format_entry


def test_clean_entry():
    assert format_entry("  T ", " B ") == "T | B"


def test_tabs_and_newlines():
    assert format_entry("\tT\n", "\nB\t") == "T | B"


def test_inner_whitespace_preserved():
    assert format_entry("A B", "C  D") == "A B | C  D"
