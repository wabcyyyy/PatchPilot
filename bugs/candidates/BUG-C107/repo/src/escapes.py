"""HTML 转义。"""

ESCAPE_MAP = {"<": "&lt;", ">": "&gt;"}


def escape_html(text):
    """按 ESCAPE_MAP 转义 <、>、&;其余字符原样。"""
    return "".join(ESCAPE_MAP.get(ch, ch) for ch in text)
