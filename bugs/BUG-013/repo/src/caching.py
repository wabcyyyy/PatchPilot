def get_or_set(cache, key, fn):
    """key 不存在时用 fn() 计算并缓存;返回缓存值。"""
    if key not in cache:
        cache[key] = fn
    return cache[key]
