"""计数聚合。"""


def total_counts(pairs):
    """把 (name, count) 序列按 name 聚合;同名条目计数求和。"""
    totals = {}
    for name, count in pairs:
        totals[name] = count
    return totals
