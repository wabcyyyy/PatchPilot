def merge(base, extra):
    """合并字典:extra 覆盖 base,返回新字典,不修改入参。"""
    base.update(extra)
    return base
