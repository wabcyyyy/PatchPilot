"""任务分发:为每个任务构造处理回调。"""


def build_handlers(names):
    """为每个任务名构造一个回调;调用回调返回它对应的任务名。"""
    return [lambda: name for name in names]  # noqa: B023 (标记的正是本题缺陷)
