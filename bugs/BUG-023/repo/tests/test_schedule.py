from datetime import UTC, datetime, timedelta

from src.schedule import is_due


def test_aware_deadline_past():
    past = datetime.now(UTC) - timedelta(hours=1)
    assert is_due(past) is True


def test_aware_deadline_future():
    future = datetime.now(UTC) + timedelta(hours=1)
    assert is_due(future) is False


def test_explicit_now_comparison():
    deadline = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert is_due(deadline, now) is True


def test_naive_deadline_treated_as_utc():
    deadline = datetime(2020, 1, 1)  # naive,早已过去
    assert is_due(deadline) is True


def test_naive_future_deadline_not_due():
    deadline = datetime.now() + timedelta(hours=1)  # naive 未来
    assert is_due(deadline) is False


def test_naive_now_compared_to_aware_deadline():
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    now = datetime(2026, 1, 1)  # naive
    assert is_due(deadline, now) is False
