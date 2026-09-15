"""条目格式化流水线。"""

from src.helpers import clean_text


def format_entry(title, body):
    """生成 "标题 | 正文" 格式的条目。"""
    return f"{clean_text(title)} | {clean_text(body)}"
