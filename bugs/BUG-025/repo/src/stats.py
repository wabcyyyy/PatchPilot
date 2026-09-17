"""数据统计。"""


def summarize(rows):
    """返回 (总和, 行数);rows 是任意可迭代对象。"""
    total = sum(rows)
    count = len(list(rows))
    return total, count
