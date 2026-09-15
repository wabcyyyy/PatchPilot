from src.fileio import read_head


def test_reads_first_lines(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("l1\nl2\nl3\nl4", encoding="utf-8")
    assert read_head(f, 2) == ["l1", "l2"]


def test_missing_file_returns_empty(tmp_path):
    assert read_head(tmp_path / "nope.txt", 3) == []


def test_n_larger_than_file(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("a", encoding="utf-8")
    assert read_head(f, 10) == ["a"]
