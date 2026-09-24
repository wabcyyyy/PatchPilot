"""CSV 导出。"""


def to_csv(rows):
    """把二维行集导出为 CSV 文本;需要转义的单元格必须先过 escaping.escape_cell。"""
    return "\n".join(",".join(str(cell) for cell in row) for row in rows)
