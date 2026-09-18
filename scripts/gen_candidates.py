"""Hard 候选题生成器(T10.2):BUG-C1xx 多文件/协同题,产物入 bugs/candidates/。

与 gen_bugs.py 的 spec 同构,差异:
- fixes: [{module, buggy_code, fixed_code}, ...] 支持一次修复多个文件(reference.diff 逐文件拼接);
- 产物写入 bugs/candidates/,不进正式集(list_bug_ids 只枚举 bugs/BUG-* 目录);
- 候选题必须经 scripts/validate_candidate.py 验证,并人工审题后才可转正。

用法:
    python scripts/gen_candidates.py            # 生成全部候选
    python scripts/gen_candidates.py BUG-C101   # 生成单题
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_bugs import gen_bug  # noqa: E402

CANDIDATES = ROOT / "bugs" / "candidates"

# ---------------------------------------------------------------------------
# 候选题 spec
# ---------------------------------------------------------------------------

C101 = {
    "id": "BUG-C101",
    "category": "跨文件定位",
    "difficulty": "hard",
    "issue": (
        "报表函数 render_counts(src/report.py)输出的计数汇总不对:同名条目没有累加,"
        "只显示了最后一次的值。症状在 report 层,根因可能在它依赖的模块里。"
        "请定位根因并修复,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/aggregator.py",
            "buggy_code": '''"""计数聚合。"""


def total_counts(pairs):
    """把 (name, count) 序列按 name 聚合;同名条目计数求和。"""
    totals = {}
    for name, count in pairs:
        totals[name] = count
    return totals
''',
            "fixed_code": '''"""计数聚合。"""


def total_counts(pairs):
    """把 (name, count) 序列按 name 聚合;同名条目计数求和。"""
    totals = {}
    for name, count in pairs:
        totals[name] = totals.get(name, 0) + count
    return totals
''',
        }
    ],
    "extra_files": {
        "src/summary.py": '''"""计数汇总行。"""

from src.aggregator import total_counts


def summary_lines(pairs):
    """聚合并按名称排序,生成 name: count 行列表。"""
    return [f"{name}: {count}" for name, count in sorted(total_counts(pairs).items())]
''',
        "src/report.py": '''"""报表输出。"""

from src.summary import summary_lines


def render_counts(pairs):
    """渲染计数汇总报表(按名称排序,每行一条)。"""
    return "\\n".join(summary_lines(pairs))
''',
    },
    "test_path": "tests/test_report_counts.py",
    "test_code": """from src.report import render_counts


def test_duplicated_names_are_summed():
    assert render_counts([("b", 2), ("a", 1), ("b", 3)]) == "a: 1\\nb: 5"


def test_unique_names_kept():
    assert render_counts([("api", 2), ("web", 1)]) == "api: 2\\nweb: 1"


def test_empty_input():
    assert render_counts([]) == ""


def test_zero_count_is_kept():
    assert render_counts([("cache", 0)]) == "cache: 0"
""",
    "failed": ["test_duplicated_names_are_summed"],
    "regression": ["test_unique_names_kept", "test_empty_input", "test_zero_count_is_kept"],
    "allowed_paths": ["src/aggregator.py", "src/summary.py", "src/report.py"],
    "search_hint": "total_counts",
}

C102 = {
    "id": "BUG-C102",
    "category": "跨文件协同",
    "difficulty": "hard",
    "issue": (
        "订单结算 order_total(src/checkout.py)的总额与手工核算对不上,且大件订单的运费"
        "没有按规则封顶。两处规则都要对齐:总额必须包含按总重量计的运费,"
        "超重运费必须封顶。请修复,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/fees.py",
            "buggy_code": '''"""运费规则。"""

BASE_SHIPPING = 8.0
PER_KG = 1.5
HEAVY_WEIGHT_KG = 10.0
HEAVY_SHIPPING_CAP = 20.0


def shipping_for(weight_kg):
    """运费 = 基础价 + 按公斤计费;重量超过 HEAVY_WEIGHT_KG 时封顶 HEAVY_SHIPPING_CAP。"""
    return round(BASE_SHIPPING + PER_KG * weight_kg, 2)
''',
            "fixed_code": '''"""运费规则。"""

BASE_SHIPPING = 8.0
PER_KG = 1.5
HEAVY_WEIGHT_KG = 10.0
HEAVY_SHIPPING_CAP = 20.0


def shipping_for(weight_kg):
    """运费 = 基础价 + 按公斤计费;重量超过 HEAVY_WEIGHT_KG 时封顶 HEAVY_SHIPPING_CAP。"""
    fee = BASE_SHIPPING + PER_KG * weight_kg
    if weight_kg > HEAVY_WEIGHT_KG:
        fee = min(fee, HEAVY_SHIPPING_CAP)
    return round(fee, 2)
''',
        },
        {
            "module": "src/checkout.py",
            "buggy_code": '''"""订单结算。"""


def order_total(items):
    """订单总额 = Σ(单价×数量) + 运费;items 为 (名称, 单价, 数量, 单件重量kg)。"""
    subtotal = sum(price * qty for _, price, qty, _ in items)
    return round(subtotal, 2)
''',
            "fixed_code": '''"""订单结算。"""

from src.fees import shipping_for


def order_total(items):
    """订单总额 = Σ(单价×数量) + 运费;items 为 (名称, 单价, 数量, 单件重量kg)。"""
    subtotal = sum(price * qty for _, price, qty, _ in items)
    total_weight = sum(weight * qty for _, _, qty, weight in items)
    return round(subtotal + shipping_for(total_weight), 2)
''',
        },
    ],
    "test_path": "tests/test_checkout.py",
    "test_code": """from src.checkout import order_total
from src.fees import shipping_for


def test_order_includes_shipping():
    assert order_total([("desk", 50.0, 1, 12.0)]) == 70.0


def test_heavy_multi_quantity_order():
    # 2 × 6kg = 12kg,运费封顶 20;小计 60
    assert order_total([("lamp", 30.0, 2, 6.0)]) == 80.0


def test_shipping_capped_for_heavy():
    # 8 + 1.5 × 12 = 26,封顶 20
    assert shipping_for(12.0) == 20.0


def test_light_shipping_uncapped():
    assert shipping_for(1.0) == 9.5


def test_zero_weight_shipping():
    assert shipping_for(0.0) == 8.0
""",
    "failed": [
        "test_order_includes_shipping",
        "test_heavy_multi_quantity_order",
        "test_shipping_capped_for_heavy",
    ],
    "regression": ["test_light_shipping_uncapped", "test_zero_weight_shipping"],
    "allowed_paths": ["src/fees.py", "src/checkout.py"],
    "search_hint": "shipping_for",
}

SPECS = [C101, C102]


def main(argv: list[str]) -> int:
    targets = [s for s in SPECS if not argv or s["id"] in argv]
    if not targets:
        print("no candidate spec matched:", argv)
        return 1
    for spec in targets:
        gen_bug(spec, root=CANDIDATES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
