"""分页工具。"""


def page_slice(items, page, size):
    """返回第 page 页(1-based),每页 size 条;越界页返回空列表。"""
    if page < 1 or size < 1:
        raise ValueError("page and size must be >= 1")
    start = (page - 1) * size
    end = start + size - 1
    return list(items[start:end])
