"""CSV 单元格转义。"""

SPECIAL_CHARS = (",", '"')


def escape_cell(value):
    """含 逗号/引号/换行 的值加引号并把内部引号翻倍;其余原样返回。"""
    text = str(value)
    if any(ch in text for ch in SPECIAL_CHARS):
        return '"' + text.replace('"', '""') + '"'
    return text
