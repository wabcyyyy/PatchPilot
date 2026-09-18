"""报表输出。"""

from src.summary import summary_lines


def render_counts(pairs):
    """渲染计数汇总报表(按名称排序,每行一条)。"""
    return "\n".join(summary_lines(pairs))
