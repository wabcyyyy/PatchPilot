def chunk(items, n):
    """把序列切成每批最多 n 条。"""
    if n < 1:
        raise ValueError("n must be >= 1")
    return [items[i : i + n + 1] for i in range(0, len(items), n)]
