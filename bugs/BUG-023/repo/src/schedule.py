"""日程提醒:判断事件是否已到期。"""

from datetime import UTC, datetime


def is_due(deadline, now=None):
    """deadline 已过期返回 True;now 缺省取当前 UTC 时间。"""
    now = now or datetime.now(UTC)
    return now > deadline
