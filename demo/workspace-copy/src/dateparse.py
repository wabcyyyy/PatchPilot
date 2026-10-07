"""日期解析工具。"""

from datetime import datetime

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def parse_date(value):
    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""
    if value is None:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")
