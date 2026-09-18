from src.ratelimit import RateLimiter
from src.units import seconds_to_ticks


def test_seconds_to_ticks():
    assert seconds_to_ticks(60) == 6000


def test_allows_up_to_limit_then_blocks():
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert limiter.allow(0)
    assert limiter.allow(1000)
    assert limiter.allow(2000)
    assert not limiter.allow(5900)


def test_exact_boundary_is_inside_window():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow(0)
    assert not limiter.allow(6000)


def test_window_expiry_allows_again():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow(0)
    assert limiter.allow(6001)
