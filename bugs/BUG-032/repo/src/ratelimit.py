"""接口限流:滑动窗口计数。"""

from src.window import count_within


class RateLimiter:
    """window_seconds 内最多放行 limit 次。"""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_seconds = window_seconds
        self.stamps = []

    def allow(self, now_tick):
        """now_tick 时刻是否放行;放行则记录本次。"""
        if count_within(self.stamps, self.window_seconds, now_tick) >= self.limit:
            return False
        self.stamps.append(now_tick)
        return True
