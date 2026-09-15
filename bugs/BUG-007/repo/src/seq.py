def middle(items):
    """取中位元素;偶数长度取靠前的一个。"""
    if not items:
        raise ValueError("empty sequence")
    return items[len(items) // 2]
