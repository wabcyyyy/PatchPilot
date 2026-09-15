"""日期解析工具。

``parse_date`` 的约定:``None`` 与空/空白字符串都返回 ``None``,
合法格式返回 ``datetime.date``,其余输入抛 ``ValueError``。

已知 Bug:空字符串与空白字符串未做防御,直接进入格式循环后抛出 ``ValueError``。
"""

from __future__ import annotations

from datetime import date, datetime

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y")


def parse_date(value: str | None) -> date | None:
    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""
    if value is None:
        return None
    # BUG: 空字符串/空白字符串未处理,strptime 抛 ValueError 直接冒泡
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")
