from src.dispatch import build_handlers


def test_handlers_return_right_count():
    handlers = build_handlers(["a", "b", "c"])
    assert len(handlers) == 3
    assert all(callable(h) for h in handlers)


def test_single_task_handler():
    (handler,) = build_handlers(["only"])
    assert handler() == "only"


def test_each_handler_knows_its_task():
    handlers = build_handlers(["a", "b", "c"])
    assert [h() for h in handlers] == ["a", "b", "c"]
