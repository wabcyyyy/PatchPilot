from src.stats import summarize


def test_summarize_list():
    assert summarize([1, 2, 3]) == (6, 3)


def test_summarize_tuple():
    assert summarize((1, 2)) == (3, 2)


def test_summarize_generator():
    assert summarize(x for x in (1, 2, 3)) == (6, 3)


def test_summarize_map_object():
    assert summarize(map(lambda x: x * 2, (1, 2))) == (6, 2)
