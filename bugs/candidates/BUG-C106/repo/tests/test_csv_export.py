from src.escaping import escape_cell
from src.exporter import to_csv


def test_comma_cell_is_quoted():
    assert to_csv([["a,b", "c"]]) == '"a,b",c'


def test_newline_cell_is_quoted():
    assert to_csv([["line1\nline2", "x"]]) == '"line1\nline2",x'


def test_quote_cell_is_doubled():
    assert to_csv([['say "hi"']]) == '"say ""hi"""'


def test_plain_cells_unchanged():
    assert to_csv([["a", "b"], [1, 2]]) == "a,b\n1,2"


def test_empty_rows():
    assert to_csv([]) == ""


def test_escape_cell_plain():
    assert escape_cell("plain") == "plain"


def test_escape_cell_quote():
    assert escape_cell('a"b') == '"a""b"'
