"""计数汇总行。"""

from src.aggregator import total_counts


def summary_lines(pairs):
    """聚合并按名称排序,生成 name: count 行列表。"""
    return [f"{name}: {count}" for name, count in sorted(total_counts(pairs).items())]
