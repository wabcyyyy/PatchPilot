"""笔记收藏工具。"""


def add_note(note, notes=[]):  # noqa: B006 (标记的正是本题缺陷)
    """把一条笔记加入收藏;未提供列表时使用默认收藏夹。"""
    notes.append(note)
    return notes
