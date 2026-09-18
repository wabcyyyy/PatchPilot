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

C103 = {
    "id": "BUG-C103",
    "category": "对照定位",
    "difficulty": "hard",
    "issue": (
        "路径段解码函数 decode_segment(src/dec.py)与编码函数 encode_segment(src/enc.py)"
        "应严格互逆,但包含加号或多字节字符的路径段解码结果不对。"
        "请以 enc 的编码规则为准修复 dec,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/dec.py",
            "buggy_code": '''"""路径段解码,与 enc.encode_segment 互逆。"""


def decode_segment(text):
    """解码 %XX 序列;'+' 按字面处理,多字节 UTF-8 序列合并为单字符。"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "+":
            out.append(" ")
            i += 1
        elif ch == "%" and i + 3 <= len(text):
            out.append(chr(int(text[i + 1 : i + 3], 16)))
            i += 3
        else:
            out.append(ch)
            i += 1
    return "".join(out)
''',
            "fixed_code": '''"""路径段解码,与 enc.encode_segment 互逆。"""


def decode_segment(text):
    """解码 %XX 序列;'+' 按字面处理,多字节 UTF-8 序列合并为单字符。"""
    out = []
    pending = bytearray()
    i = 0

    def flush():
        if pending:
            out.append(pending.decode("utf-8"))
            pending.clear()

    while i < len(text):
        ch = text[i]
        if ch == "%" and i + 3 <= len(text):
            pending.append(int(text[i + 1 : i + 3], 16))
            i += 3
        else:
            flush()
            out.append(ch)
            i += 1
    flush()
    return "".join(out)
''',
        }
    ],
    "extra_files": {
        "src/enc.py": '''"""路径段编码。"""

SAFE_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-~")


def encode_segment(text):
    """路径段编码:SAFE_CHARS 之外的字符逐字节转 %XX(大写十六进制)。"""
    out = []
    for ch in text:
        if ch in SAFE_CHARS:
            out.append(ch)
        else:
            out.append("".join(f"%{byte:02X}" for byte in ch.encode("utf-8")))
    return "".join(out)
''',
    },
    "test_path": "tests/test_codec.py",
    "test_code": """from src.dec import decode_segment
from src.enc import encode_segment


def test_roundtrip_multibyte():
    for text in ("你好", "café", "a/b c"):
        assert decode_segment(encode_segment(text)) == text


def test_plus_is_literal():
    assert decode_segment("a+b") == "a+b"


def test_multibyte_escape_decoded():
    assert decode_segment("%E4%BD%A0%E5%A5%BD") == "你好"


def test_plain_passthrough():
    assert decode_segment("plain-path.txt") == "plain-path.txt"


def test_single_byte_escape():
    assert decode_segment("%2F") == "/"


def test_encode_is_stable():
    assert encode_segment("a b") == "a%20b"
""",
    "failed": [
        "test_roundtrip_multibyte",
        "test_plus_is_literal",
        "test_multibyte_escape_decoded",
    ],
    "regression": ["test_plain_passthrough", "test_single_byte_escape", "test_encode_is_stable"],
    "allowed_paths": ["src/dec.py", "src/enc.py"],
    "search_hint": "decode_segment",
}

C104 = {
    "id": "BUG-C104",
    "category": "跨文件定位",
    "difficulty": "hard",
    "issue": (
        "接口限流器 RateLimiter(src/ratelimit.py)行为异常:60 秒窗口内远没到限额就拒绝请求,"
        "窗口边界判定也和契约不符。时间在系统内以节拍表示(1 节拍 = 10 毫秒),"
        "请沿调用链定位根因并修复,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/units.py",
            "buggy_code": '''"""时间单位换算。"""

TICK_MS = 10  # 1 节拍 = 10 毫秒


def seconds_to_ticks(seconds):
    """秒 → 节拍数。"""
    return int(seconds)
''',
            "fixed_code": '''"""时间单位换算。"""

TICK_MS = 10  # 1 节拍 = 10 毫秒


def seconds_to_ticks(seconds):
    """秒 → 节拍数。"""
    return int(seconds * 1000 / TICK_MS)
''',
        }
    ],
    "extra_files": {
        "src/window.py": '''"""滑动窗口计数(时间一律用节拍表示)。"""

from src.units import seconds_to_ticks


def count_within(stamps, window_seconds, now_tick):
    """统计 stamps 中距今不超过 window_seconds 的数量(含恰好边界)。"""
    window = seconds_to_ticks(window_seconds)
    return sum(1 for ts in stamps if now_tick - ts <= window)
''',
        "src/ratelimit.py": '''"""接口限流:滑动窗口计数。"""

from src.window import count_within


class RateLimiter:
    """window_seconds 内最多放行 limit 次。"""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_seconds = window_seconds
        self.stamps = []

    def allow(self, now_tick):
        """now_tick 时刻是否放行;放行则记录本次。"""
        if count_within(self.stamps, self.window_seconds, now_tick) >= self.limit:
            return False
        self.stamps.append(now_tick)
        return True
''',
    },
    "test_path": "tests/test_ratelimit.py",
    "test_code": """from src.ratelimit import RateLimiter
from src.units import seconds_to_ticks


def test_seconds_to_ticks():
    assert seconds_to_ticks(60) == 6000


def test_allows_up_to_limit_then_blocks():
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert limiter.allow(0)
    assert limiter.allow(1000)
    assert limiter.allow(2000)
    assert not limiter.allow(5900)


def test_exact_boundary_is_inside_window():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow(0)
    assert not limiter.allow(6000)


def test_window_expiry_allows_again():
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow(0)
    assert limiter.allow(6001)
""",
    "failed": [
        "test_seconds_to_ticks",
        "test_allows_up_to_limit_then_blocks",
        "test_exact_boundary_is_inside_window",
    ],
    "regression": ["test_window_expiry_allows_again"],
    "allowed_paths": ["src/units.py", "src/window.py", "src/ratelimit.py"],
    "search_hint": "seconds_to_ticks",
}

C105 = {
    "id": "BUG-C105",
    "category": "跨文件协同",
    "difficulty": "hard",
    "issue": (
        "低库存预警漏报:恰好达到预警线的商品没有被预警,零库存(缺货)的商品也漏报了。"
        "预警判定分散在 rules 与 notify 两个模块,请把两处语义都对齐契约后修复,"
        "保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/rules.py",
            "buggy_code": '''"""库存预警规则。"""

LOW_STOCK_THRESHOLD = 5


def is_low(qty):
    """库存量是否达到低库存预警线(qty <= LOW_STOCK_THRESHOLD,含恰好等于)。"""
    return qty < LOW_STOCK_THRESHOLD
''',
            "fixed_code": '''"""库存预警规则。"""

LOW_STOCK_THRESHOLD = 5


def is_low(qty):
    """库存量是否达到低库存预警线(qty <= LOW_STOCK_THRESHOLD,含恰好等于)。"""
    return qty <= LOW_STOCK_THRESHOLD
''',
        },
        {
            "module": "src/notify.py",
            "buggy_code": '''"""低库存通知汇总。"""

from src.inventory import quantity
from src.rules import is_low


def low_stock_skus(catalog):
    """按目录顺序返回需要预警的商品;缺货(0)与达到预警线的都要包含,未知商品跳过。"""
    result = []
    for sku in catalog:
        qty = quantity(sku)
        if qty and is_low(qty):
            result.append(sku)
    return result
''',
            "fixed_code": '''"""低库存通知汇总。"""

from src.inventory import quantity
from src.rules import is_low


def low_stock_skus(catalog):
    """按目录顺序返回需要预警的商品;缺货(0)与达到预警线的都要包含,未知商品跳过。"""
    result = []
    for sku in catalog:
        qty = quantity(sku)
        if qty is not None and is_low(qty):
            result.append(sku)
    return result
''',
        },
    ],
    "extra_files": {
        "src/inventory.py": '''"""库存查询。"""

STOCK = {"pen": 3, "book": 0, "lamp": 10, "desk": 5}


def quantity(sku):
    """当前库存量;未知商品返回 None。"""
    return STOCK.get(sku)
''',
    },
    "test_path": "tests/test_low_stock.py",
    "test_code": """from src.notify import low_stock_skus
from src.rules import is_low


def test_out_of_stock_is_reported():
    assert low_stock_skus(["pen", "book"]) == ["pen", "book"]


def test_threshold_boundary_is_low():
    # desk 恰好 5 件,达到预警线
    assert low_stock_skus(["desk"]) == ["desk"]


def test_is_low_at_threshold():
    assert is_low(5) is True


def test_is_low_below_threshold():
    assert is_low(4) is True


def test_overstock_not_reported():
    assert low_stock_skus(["lamp"]) == []


def test_unknown_sku_skipped():
    assert low_stock_skus(["ghost"]) == []


def test_empty_catalog():
    assert low_stock_skus([]) == []
""",
    "failed": [
        "test_out_of_stock_is_reported",
        "test_threshold_boundary_is_low",
        "test_is_low_at_threshold",
    ],
    "regression": [
        "test_is_low_below_threshold",
        "test_overstock_not_reported",
        "test_unknown_sku_skipped",
        "test_empty_catalog",
    ],
    "allowed_paths": ["src/rules.py", "src/notify.py", "src/inventory.py"],
    "search_hint": "is_low",
}

C106 = {
    "id": "BUG-C106",
    "category": "跨文件协同",
    "difficulty": "hard",
    "issue": (
        "CSV 导出 to_csv(src/exporter.py)产出的文件在表格软件里串列:"
        "含逗号/引号/换行的单元格没有被正确转义。转义规则在 escaping 模块,"
        "但导出侧可能也没有按契约调用它。请把两处都对齐契约后修复,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/escaping.py",
            "buggy_code": '''"""CSV 单元格转义。"""

SPECIAL_CHARS = (",", '"')


def escape_cell(value):
    """含 逗号/引号/换行 的值加引号并把内部引号翻倍;其余原样返回。"""
    text = str(value)
    if any(ch in text for ch in SPECIAL_CHARS):
        return '"' + text.replace('"', '""') + '"'
    return text
''',
            "fixed_code": '''"""CSV 单元格转义。"""

SPECIAL_CHARS = (",", '"', "\\n")


def escape_cell(value):
    """含 逗号/引号/换行 的值加引号并把内部引号翻倍;其余原样返回。"""
    text = str(value)
    if any(ch in text for ch in SPECIAL_CHARS):
        return '"' + text.replace('"', '""') + '"'
    return text
''',
        },
        {
            "module": "src/exporter.py",
            "buggy_code": '''"""CSV 导出。"""


def to_csv(rows):
    """把二维行集导出为 CSV 文本;需要转义的单元格必须先过 escaping.escape_cell。"""
    return "\\n".join(",".join(str(cell) for cell in row) for row in rows)
''',
            "fixed_code": '''"""CSV 导出。"""

from src.escaping import escape_cell


def to_csv(rows):
    """把二维行集导出为 CSV 文本;需要转义的单元格必须先过 escaping.escape_cell。"""
    return "\\n".join(",".join(escape_cell(cell) for cell in row) for row in rows)
''',
        },
    ],
    "test_path": "tests/test_csv_export.py",
    "test_code": '''from src.escaping import escape_cell
from src.exporter import to_csv


def test_comma_cell_is_quoted():
    assert to_csv([["a,b", "c"]]) == '"a,b",c'


def test_newline_cell_is_quoted():
    assert to_csv([["line1\\nline2", "x"]]) == \'"line1\\nline2",x\'


def test_quote_cell_is_doubled():
    assert to_csv([['say "hi"']]) == \'"say ""hi"""\'


def test_plain_cells_unchanged():
    assert to_csv([["a", "b"], [1, 2]]) == "a,b\\n1,2"


def test_empty_rows():
    assert to_csv([]) == ""


def test_escape_cell_plain():
    assert escape_cell("plain") == "plain"


def test_escape_cell_quote():
    assert escape_cell('a"b') == '"a""b"'
''',
    "failed": [
        "test_comma_cell_is_quoted",
        "test_newline_cell_is_quoted",
        "test_quote_cell_is_doubled",
    ],
    "regression": [
        "test_plain_cells_unchanged",
        "test_empty_rows",
        "test_escape_cell_plain",
        "test_escape_cell_quote",
    ],
    "allowed_paths": ["src/escaping.py", "src/exporter.py"],
    "search_hint": "escape_cell",
}

C107 = {
    "id": "BUG-C107",
    "category": "跨文件协同",
    "difficulty": "hard",
    "issue": (
        "模板渲染 render(src/template.py)有两个问题:替换值为 None 时输出了字面量 None"
        "(契约应渲染为空串),且替换值里的 & 字符没有被转义。两处分别位于 template 与"
        " escapes 模块,请对齐契约修复,保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/escapes.py",
            "buggy_code": '''"""HTML 转义。"""

ESCAPE_MAP = {"<": "&lt;", ">": "&gt;"}


def escape_html(text):
    """按 ESCAPE_MAP 转义 <、>、&;其余字符原样。"""
    return "".join(ESCAPE_MAP.get(ch, ch) for ch in text)
''',
            "fixed_code": '''"""HTML 转义。"""

ESCAPE_MAP = {"<": "&lt;", ">": "&gt;", "&": "&amp;"}


def escape_html(text):
    """按 ESCAPE_MAP 转义 <、>、&;其余字符原样。"""
    return "".join(ESCAPE_MAP.get(ch, ch) for ch in text)
''',
        },
        {
            "module": "src/template.py",
            "buggy_code": '''"""极简模板:{{key}} 占位符替换。"""

from src.escapes import escape_html

AUTOESCAPE = True


def render(template, ctx, autoescape=AUTOESCAPE):
    """替换 {{key}};autoescape 开启时替换值做 HTML 转义;None 渲染为空串。"""
    out = template
    for key, value in ctx.items():
        token = "{{" + key + "}}"
        replacement = escape_html(str(value)) if autoescape else str(value)
        out = out.replace(token, replacement)
    return out
''',
            "fixed_code": '''"""极简模板:{{key}} 占位符替换。"""

from src.escapes import escape_html

AUTOESCAPE = True


def render(template, ctx, autoescape=AUTOESCAPE):
    """替换 {{key}};autoescape 开启时替换值做 HTML 转义;None 渲染为空串。"""
    out = template
    for key, value in ctx.items():
        token = "{{" + key + "}}"
        text = "" if value is None else str(value)
        replacement = escape_html(text) if autoescape else text
        out = out.replace(token, replacement)
    return out
''',
        },
    ],
    "test_path": "tests/test_template.py",
    "test_code": """from src.escapes import escape_html
from src.template import render


def test_none_renders_empty():
    assert render("Hi {{name}}!", {"name": None}) == "Hi !"


def test_ampersand_escaped():
    assert render("{{x}}", {"x": "<b>&"}) == "&lt;b&gt;&amp;"


def test_plain_replacement():
    assert render("Hi {{name}}", {"name": "Ann"}) == "Hi Ann"


def test_autoescape_off_keeps_raw():
    assert render("{{x}}", {"x": "<b>"}, autoescape=False) == "<b>"


def test_escape_html_basics():
    assert escape_html("a<b") == "a&lt;b"
    assert escape_html("a&b") == "a&amp;b"
""",
    "failed": ["test_none_renders_empty", "test_ampersand_escaped", "test_escape_html_basics"],
    "regression": ["test_plain_replacement", "test_autoescape_off_keeps_raw"],
    "allowed_paths": ["src/escapes.py", "src/template.py"],
    "search_hint": "escape_html",
}

C108 = {
    "id": "BUG-C108",
    "category": "对照定位",
    "difficulty": "hard",
    "issue": (
        "fast_checksum(src/checksum.py)是参考实现 checksum_ref(src/checksum_ref.py,"
        "语义权威)的快速版,但对某些输入两者结果不一致。请对照参考实现找出分歧并修复,"
        "保证原有测试通过。"
    ),
    "fixes": [
        {
            "module": "src/checksum.py",
            "buggy_code": '''"""快速校验和:语义必须与 checksum_ref.checksum 一致。"""


def fast_checksum(data):
    """Σ(UTF-8 字节) mod 65536 的快速实现。"""
    return sum(data.encode("ascii", errors="ignore")) % 65536
''',
            "fixed_code": '''"""快速校验和:语义必须与 checksum_ref.checksum 一致。"""


def fast_checksum(data):
    """Σ(UTF-8 字节) mod 65536 的快速实现。"""
    return sum(data.encode("utf-8")) % 65536
''',
        }
    ],
    "extra_files": {
        "src/checksum_ref.py": '''"""参考实现:大输入较慢,但语义权威。"""


def checksum(data):
    """Σ(UTF-8 字节) mod 65536,逐字节取模的朴素实现。"""
    total = 0
    for byte in data.encode("utf-8"):
        total = (total + byte) % 65536
    return total
''',
    },
    "test_path": "tests/test_checksum.py",
    "test_code": """from src.checksum import fast_checksum
from src.checksum_ref import checksum


def test_matches_reference_on_multibyte():
    for text in ("héllo", "你好", "naïve café"):
        assert fast_checksum(text) == checksum(text)


def test_matches_reference_on_ascii():
    for text in ("", "hello", "PatchPilot-2026"):
        assert fast_checksum(text) == checksum(text)


def test_reference_known_value():
    # A=65, B=66
    assert checksum("AB") == 131


def test_fast_known_value():
    assert fast_checksum("AB") == 131


def test_multibyte_changes_result():
    # é 是两个 UTF-8 字节,校验和必须受它影响
    assert fast_checksum("héllo") != fast_checksum("hllo")
""",
    "failed": ["test_matches_reference_on_multibyte", "test_multibyte_changes_result"],
    "regression": [
        "test_matches_reference_on_ascii",
        "test_reference_known_value",
        "test_fast_known_value",
    ],
    "allowed_paths": ["src/checksum.py", "src/checksum_ref.py"],
    "search_hint": "fast_checksum",
}

SPECS = [C101, C102, C103, C104, C105, C106, C107, C108]


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
