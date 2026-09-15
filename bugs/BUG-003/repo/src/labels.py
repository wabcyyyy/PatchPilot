"""标签拼接工具。"""


def join_labels(labels, sep=None):
    """把标签列表连成字符串;sep 缺省为逗号。"""
    result = ""
    for index, label in enumerate(labels):
        if index > 0:
            result += sep
        result += label
    return result
