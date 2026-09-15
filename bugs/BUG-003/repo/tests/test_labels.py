from src.labels import join_labels


def test_default_separator():
    assert join_labels(["a", "b", "c"]) == "a,b,c"


def test_custom_separator():
    assert join_labels(["a", "b"], sep=" | ") == "a | b"


def test_empty_list():
    assert join_labels([]) == ""


def test_single_label():
    assert join_labels(["solo"]) == "solo"
