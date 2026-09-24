"""滑动窗口计数(时间一律用节拍表示)。"""

from src.units import seconds_to_ticks


def count_within(stamps, window_seconds, now_tick):
    """统计 stamps 中距今不超过 window_seconds 的数量(含恰好边界)。"""
    window = seconds_to_ticks(window_seconds)
    return sum(1 for ts in stamps if now_tick - ts <= window)
