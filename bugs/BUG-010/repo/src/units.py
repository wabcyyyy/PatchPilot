def to_percent(fraction):
    """比例转百分比(保留 1 位小数),并夹在 [0, 100]。"""
    return round(fraction * 100, 1)
