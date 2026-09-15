"""Bug 任务集生成器:把题目 spec 物化为 bugs/ 目录(repo + issue + manifest + 回放脚本)。

用法:
    python scripts/gen_bugs.py            # 生成全部
    python scripts/gen_bugs.py BUG-001    # 生成单题
    python scripts/gen_bugs.py --validate # 生成后跑基线校验

设计:题目源码以 spec(脆弱代码/修复代码/测试)维护在本脚本中;
diff 由 difflib 现算,回放脚本由通用模板生成——改题只改 spec。
"""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUGS = ROOT / "bugs"

CONTEST = ""  # repo 根 conftest.py 内容(空文件,让 pytest 把 repo 根加进 sys.path)


def _conftest() -> str:
    return '"""题目仓库根 conftest:让 `from src...` 导入生效(由生成器写入)。"""\n'


# ---------------------------------------------------------------------------
# 题目 spec
# ---------------------------------------------------------------------------

BUG_001 = {
    "id": "BUG-001",
    "category": "异常处理",
    "difficulty": "simple",
    "issue": (
        "仓库中的日期解析函数 `parse_date`(src/dateparse.py)在输入空字符串或纯空白字符串时"
        "抛出 ValueError。按函数契约,空输入应当返回 None。请修复,并保证原有测试全部通过。"
    ),
    "module": "src/dateparse.py",
    "buggy_code": '''"""日期解析工具。"""

from datetime import date, datetime

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def parse_date(value):
    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""
    if value is None:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")
''',
    "fixed_code": '''"""日期解析工具。"""

from datetime import date, datetime

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")


def parse_date(value):
    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""
    if value is None:
        return None
    if not value.strip():
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")
''',
    "test_path": "tests/test_dateparse.py",
    "test_code": """import pytest

from src.dateparse import parse_date


def test_iso_format():
    assert parse_date("2026-01-31").isoformat() == "2026-01-31"


def test_slash_format():
    assert parse_date("2026/01/31").isoformat() == "2026-01-31"


def test_invalid_format_raises():
    with pytest.raises(ValueError):
        parse_date("31-01-2026")


def test_none_returns_none():
    assert parse_date(None) is None


def test_empty_string_returns_none():
    assert parse_date("") is None


def test_whitespace_returns_none():
    assert parse_date("   ") is None
""",
    "failed": ["test_empty_string_returns_none", "test_whitespace_returns_none"],
    "regression": [
        "test_iso_format",
        "test_slash_format",
        "test_invalid_format_raises",
        "test_none_returns_none",
    ],
    "allowed_paths": ["src/**"],
    "search_hint": "parse_date",
}

BUG_002 = {
    "id": "BUG-002",
    "category": "边界条件",
    "difficulty": "simple",
    "issue": (
        "分页函数 `page_slice`(src/pagination.py)每页会丢掉最后一条数据:"
        "page_slice(range(10), 1, 3) 应返回 [0,1,2],实际返回 [0,1]。请修复,保证原有测试通过。"
    ),
    "module": "src/pagination.py",
    "buggy_code": '''"""分页工具。"""


def page_slice(items, page, size):
    """返回第 page 页(1-based),每页 size 条;越界页返回空列表。"""
    if page < 1 or size < 1:
        raise ValueError("page and size must be >= 1")
    start = (page - 1) * size
    end = start + size - 1
    return list(items[start:end])
''',
    "fixed_code": '''"""分页工具。"""


def page_slice(items, page, size):
    """返回第 page 页(1-based),每页 size 条;越界页返回空列表。"""
    if page < 1 or size < 1:
        raise ValueError("page and size must be >= 1")
    start = (page - 1) * size
    end = start + size
    return list(items[start:end])
''',
    "test_path": "tests/test_pagination.py",
    "test_code": """import pytest

from src.pagination import page_slice

DATA = list(range(10))


def test_first_page_full():
    assert page_slice(DATA, 1, 3) == [0, 1, 2]


def test_last_page_partial():
    assert page_slice(DATA, 4, 3) == [9]


def test_invalid_page_raises():
    with pytest.raises(ValueError):
        page_slice(DATA, 0, 3)


def test_page_beyond_end_is_empty():
    assert page_slice(DATA, 99, 3) == []
""",
    "failed": ["test_first_page_full", "test_last_page_partial"],
    "regression": ["test_invalid_page_raises", "test_page_beyond_end_is_empty"],
    "allowed_paths": ["src/**"],
    "search_hint": "page_slice",
}

BUG_003 = {
    "id": "BUG-003",
    "category": "类型错误",
    "difficulty": "simple",
    "issue": (
        "标签拼接函数 `join_labels`(src/labels.py)在不传 sep 参数时抛出 TypeError"
        "(NoneType 不能与 str 相加)。按契约,sep 缺省应为逗号。请修复,保证原有测试通过。"
    ),
    "module": "src/labels.py",
    "buggy_code": '''"""标签拼接工具。"""


def join_labels(labels, sep=None):
    """把标签列表连成字符串;sep 缺省为逗号。"""
    result = ""
    for index, label in enumerate(labels):
        if index > 0:
            result += sep
        result += label
    return result
''',
    "fixed_code": '''"""标签拼接工具。"""


def join_labels(labels, sep=None):
    """把标签列表连成字符串;sep 缺省为逗号。"""
    separator = "," if sep is None else sep
    result = ""
    for index, label in enumerate(labels):
        if index > 0:
            result += separator
        result += label
    return result
''',
    "test_path": "tests/test_labels.py",
    "test_code": """from src.labels import join_labels


def test_default_separator():
    assert join_labels(["a", "b", "c"]) == "a,b,c"


def test_custom_separator():
    assert join_labels(["a", "b"], sep=" | ") == "a | b"


def test_empty_list():
    assert join_labels([]) == ""


def test_single_label():
    assert join_labels(["solo"]) == "solo"
""",
    "failed": ["test_default_separator"],
    "regression": ["test_custom_separator", "test_empty_list", "test_single_label"],
    "allowed_paths": ["src/**"],
    "search_hint": "join_labels",
}

BUG_004 = {
    "id": "BUG-004",
    "category": "数据访问",
    "difficulty": "simple",
    "issue": (
        "配置读取函数 `lookup_setting`(src/config.py)在 store 未设置某个已知配置项时"
        "直接抛 KeyError。按契约:store 优先,未设置回退 DEFAULTS,未知键返回 None。"
        "请修复,保证原有测试通过。"
    ),
    "module": "src/config.py",
    "buggy_code": '''"""配置中心。"""

DEFAULTS = {"retries": 3, "timeout": 30}


def lookup_setting(store, key):
    """读取配置项:store 优先,未设置回退 DEFAULTS,未知键返回 None。"""
    return store[key]
''',
    "fixed_code": '''"""配置中心。"""

DEFAULTS = {"retries": 3, "timeout": 30}


def lookup_setting(store, key):
    """读取配置项:store 优先,未设置回退 DEFAULTS,未知键返回 None。"""
    if key in store:
        return store[key]
    return DEFAULTS.get(key)
''',
    "test_path": "tests/test_config.py",
    "test_code": """import pytest

from src.config import DEFAULTS, lookup_setting


def test_store_value_wins():
    assert lookup_setting({"timeout": 5}, "timeout") == 5


def test_fallback_to_default():
    assert lookup_setting({}, "retries") == DEFAULTS["retries"]


def test_unknown_key_returns_none():
    assert lookup_setting({}, "nope") is None


def test_partial_store():
    assert lookup_setting({"retries": 9}, "timeout") == DEFAULTS["timeout"]
""",
    "failed": ["test_fallback_to_default", "test_unknown_key_returns_none", "test_partial_store"],
    "regression": ["test_store_value_wins"],
    "allowed_paths": ["src/**"],
    "search_hint": "lookup_setting",
}

BUG_005 = {
    "id": "BUG-005",
    "category": "跨文件定位",
    "difficulty": "medium",
    "issue": (
        "条目格式化函数 `format_entry`(src/pipeline.py)产出的文本在标题/正文含制表符或换行时"
        "没有清理干净,导致下游展示错位。症状在 pipeline,根因可能在它依赖的模块里。"
        "请定位根因并修复,保证原有测试通过。"
    ),
    "module": "src/helpers.py",
    "extra_files": {
        "src/pipeline.py": '''"""条目格式化流水线。"""

from src.helpers import clean_text


def format_entry(title, body):
    """生成 "标题 | 正文" 格式的条目。"""
    return f"{clean_text(title)} | {clean_text(body)}"
''',
    },
    "buggy_code": '''"""文本清理助手。"""


def clean_text(text):
    """压缩文本:去掉首尾的全部空白字符。"""
    return text.strip(" ")
''',
    "fixed_code": '''"""文本清理助手。"""


def clean_text(text):
    """压缩文本:去掉首尾的全部空白字符。"""
    return text.strip()
''',
    "test_path": "tests/test_pipeline.py",
    "test_code": """from src.pipeline import format_entry


def test_clean_entry():
    assert format_entry("  T ", " B ") == "T | B"


def test_tabs_and_newlines():
    assert format_entry("\\tT\\n", "\\nB\\t") == "T | B"


def test_inner_whitespace_preserved():
    assert format_entry("A B", "C  D") == "A B | C  D"
""",
    "failed": ["test_tabs_and_newlines"],
    "regression": ["test_clean_entry", "test_inner_whitespace_preserved"],
    "allowed_paths": ["src/helpers.py", "src/pipeline.py"],
    "search_hint": "clean_text",
}

BUG_006 = {
    "id": "BUG-006",
    "category": "异常处理",
    "difficulty": "simple",
    "issue": "`safe_divide`(src/math_ops.py)在除数为 0 时抛 ZeroDivisionError,契约要求返回 None。请修复。",
    "module": "src/math_ops.py",
    "buggy_code": '''def safe_divide(a, b):
    """安全除法:除数为 0 返回 None,其余返回 a/b。"""
    return a / b
''',
    "fixed_code": '''def safe_divide(a, b):
    """安全除法:除数为 0 返回 None,其余返回 a/b。"""
    if b == 0:
        return None
    return a / b
''',
    "test_path": "tests/test_math_ops.py",
    "test_code": """from src.math_ops import safe_divide


def test_normal_division():
    assert safe_divide(6, 3) == 2.0


def test_zero_divisor_returns_none():
    assert safe_divide(1, 0) is None


def test_negative_divisor():
    assert safe_divide(6, -3) == -2.0


def test_float_result():
    assert safe_divide(1, 4) == 0.25
""",
    "failed": ["test_zero_divisor_returns_none"],
    "regression": ["test_normal_division", "test_negative_divisor", "test_float_result"],
    "allowed_paths": ["src/**"],
    "search_hint": "safe_divide",
}

BUG_007 = {
    "id": "BUG-007",
    "category": "边界条件",
    "difficulty": "simple",
    "issue": "`middle`(src/seq.py)取序列中位元素;偶数长度时契约取靠前的那个。当前偶数长度输入取错了。请修复。",
    "module": "src/seq.py",
    "buggy_code": '''def middle(items):
    """取中位元素;偶数长度取靠前的一个。"""
    if not items:
        raise ValueError("empty sequence")
    return items[len(items) // 2]
''',
    "fixed_code": '''def middle(items):
    """取中位元素;偶数长度取靠前的一个。"""
    if not items:
        raise ValueError("empty sequence")
    return items[(len(items) - 1) // 2]
''',
    "test_path": "tests/test_seq.py",
    "test_code": """import pytest

from src.seq import middle


def test_odd_length():
    assert middle([1, 2, 3]) == 2


def test_even_length_takes_earlier():
    assert middle([1, 2, 3, 4]) == 2


def test_single_element():
    assert middle([9]) == 9


def test_empty_raises():
    with pytest.raises(ValueError):
        middle([])
""",
    "failed": ["test_even_length_takes_earlier"],
    "regression": ["test_odd_length", "test_single_element", "test_empty_raises"],
    "allowed_paths": ["src/**"],
    "search_hint": "def middle",
}

BUG_008 = {
    "id": "BUG-008",
    "category": "类型错误",
    "difficulty": "simple",
    "issue": "`count_vowels`(src/text_stats.py)在输入 None 时抛 TypeError,契约要求按 0 处理。请修复。",
    "module": "src/text_stats.py",
    "buggy_code": '''VOWELS = set("aeiouAEIOU")


def count_vowels(text):
    """统计元音字母个数;None 视为 0。"""
    return sum(1 for ch in text if ch in VOWELS)
''',
    "fixed_code": '''VOWELS = set("aeiouAEIOU")


def count_vowels(text):
    """统计元音字母个数;None 视为 0。"""
    if text is None:
        return 0
    return sum(1 for ch in text if ch in VOWELS)
''',
    "test_path": "tests/test_text_stats.py",
    "test_code": """from src.text_stats import count_vowels


def test_counts_vowels():
    assert count_vowels("PatchPilot") == 3


def test_none_counts_zero():
    assert count_vowels(None) == 0


def test_no_vowels():
    assert count_vowels("rhythm") == 0


def test_empty_string():
    assert count_vowels("") == 0
""",
    "failed": ["test_none_counts_zero"],
    "regression": ["test_counts_vowels", "test_no_vowels", "test_empty_string"],
    "allowed_paths": ["src/**"],
    "search_hint": "count_vowels",
}

BUG_009 = {
    "id": "BUG-009",
    "category": "数据访问",
    "difficulty": "simple",
    "issue": "`merge`(src/dictops.py)合并两个字典时应返回新字典且不修改入参;当前实现污染了第一个入参。请修复。",
    "module": "src/dictops.py",
    "buggy_code": '''def merge(base, extra):
    """合并字典:extra 覆盖 base,返回新字典,不修改入参。"""
    base.update(extra)
    return base
''',
    "fixed_code": '''def merge(base, extra):
    """合并字典:extra 覆盖 base,返回新字典,不修改入参。"""
    merged = dict(base)
    merged.update(extra)
    return merged
''',
    "test_path": "tests/test_dictops.py",
    "test_code": """from src.dictops import merge


def test_extra_overrides_base():
    assert merge({"a": 1, "b": 2}, {"b": 9}) == {"a": 1, "b": 9}


def test_inputs_not_mutated():
    base = {"a": 1}
    merge(base, {"b": 2})
    assert base == {"a": 1}


def test_empty_extra():
    assert merge({"a": 1}, {}) == {"a": 1}
""",
    "failed": ["test_inputs_not_mutated"],
    "regression": ["test_extra_overrides_base", "test_empty_extra"],
    "allowed_paths": ["src/**"],
    "search_hint": "def merge",
}

BUG_010 = {
    "id": "BUG-010",
    "category": "边界条件",
    "difficulty": "simple",
    "issue": "`to_percent`(src/units.py)应把比例转成百分比并夹在 [0, 100];当前超出范围的输入没有被夹住。请修复。",
    "module": "src/units.py",
    "buggy_code": '''def to_percent(fraction):
    """比例转百分比(保留 1 位小数),并夹在 [0, 100]。"""
    return round(fraction * 100, 1)
''',
    "fixed_code": '''def to_percent(fraction):
    """比例转百分比(保留 1 位小数),并夹在 [0, 100]。"""
    return round(min(100.0, max(0.0, fraction * 100)), 1)
''',
    "test_path": "tests/test_units.py",
    "test_code": """from src.units import to_percent


def test_normal_value():
    assert to_percent(0.123) == 12.3


def test_above_range_clamped():
    assert to_percent(1.5) == 100.0


def test_negative_clamped():
    assert to_percent(-0.2) == 0.0


def test_boundary_one():
    assert to_percent(1.0) == 100.0
""",
    "failed": ["test_above_range_clamped", "test_negative_clamped"],
    "regression": ["test_normal_value", "test_boundary_one"],
    "allowed_paths": ["src/**"],
    "search_hint": "to_percent",
}

BUG_011 = {
    "id": "BUG-011",
    "category": "异常处理",
    "difficulty": "simple",
    "issue": "`parse_int`(src/strconv.py)解析失败时应返回 default(缺省 None),当前直接抛 ValueError。请修复。",
    "module": "src/strconv.py",
    "buggy_code": '''def parse_int(text, default=None):
    """把字符串解析为 int;失败返回 default。"""
    return int(text)
''',
    "fixed_code": '''def parse_int(text, default=None):
    """把字符串解析为 int;失败返回 default。"""
    try:
        return int(text)
    except (TypeError, ValueError):
        return default
''',
    "test_path": "tests/test_strconv.py",
    "test_code": """from src.strconv import parse_int


def test_valid_number():
    assert parse_int("42") == 42


def test_invalid_returns_default():
    assert parse_int("abc") is None


def test_custom_default():
    assert parse_int("x", default=-1) == -1


def test_whitespace_number():
    assert parse_int(" 7 ") == 7
""",
    "failed": ["test_invalid_returns_default", "test_custom_default"],
    "regression": ["test_valid_number", "test_whitespace_number"],
    "allowed_paths": ["src/**"],
    "search_hint": "parse_int",
}

BUG_012 = {
    "id": "BUG-012",
    "category": "类型错误",
    "difficulty": "simple",
    "issue": "`concat`(src/collections_ops.py)拼接两个序列;当传入 list 与 tuple 混合时抛 TypeError。契约要求支持任意序列并返回 list。请修复。",
    "module": "src/collections_ops.py",
    "buggy_code": '''def concat(first, second):
    """拼接两个序列,返回 list。"""
    return first + second
''',
    "fixed_code": '''def concat(first, second):
    """拼接两个序列,返回 list。"""
    return list(first) + list(second)
''',
    "test_path": "tests/test_collections_ops.py",
    "test_code": """from src.collections_ops import concat


def test_two_lists():
    assert concat([1], [2, 3]) == [1, 2, 3]


def test_list_and_tuple():
    assert concat([1], (2, 3)) == [1, 2, 3]


def test_strings_sequence():
    assert concat("ab", "cd") == ["a", "b", "c", "d"]


def test_empty_inputs():
    assert concat([], ()) == []
""",
    "failed": ["test_list_and_tuple", "test_strings_sequence", "test_empty_inputs"],
    "regression": ["test_two_lists"],
    "allowed_paths": ["src/**"],
    "search_hint": "def concat",
}

BUG_013 = {
    "id": "BUG-013",
    "category": "数据访问",
    "difficulty": "simple",
    "issue": "`get_or_set`(src/caching.py)应把 fn() 的返回值写入缓存;当前把函数本身写了进去。请修复。",
    "module": "src/caching.py",
    "buggy_code": '''def get_or_set(cache, key, fn):
    """key 不存在时用 fn() 计算并缓存;返回缓存值。"""
    if key not in cache:
        cache[key] = fn
    return cache[key]
''',
    "fixed_code": '''def get_or_set(cache, key, fn):
    """key 不存在时用 fn() 计算并缓存;返回缓存值。"""
    if key not in cache:
        cache[key] = fn()
    return cache[key]
''',
    "test_path": "tests/test_caching.py",
    "test_code": """from src.caching import get_or_set


def test_computes_and_caches_value():
    cache = {}
    assert get_or_set(cache, "k", lambda: 42) == 42
    assert cache["k"] == 42


def test_fn_called_once():
    calls = []

    def factory():
        calls.append(1)
        return "v"

    cache = {}
    get_or_set(cache, "k", factory)
    get_or_set(cache, "k", factory)
    assert len(calls) == 1


def test_existing_value_returned():
    assert get_or_set({"k": 7}, "k", lambda: 1) == 7
""",
    "failed": ["test_computes_and_caches_value", "test_fn_called_once"],
    "regression": ["test_existing_value_returned"],
    "allowed_paths": ["src/**"],
    "search_hint": "get_or_set",
}

BUG_014 = {
    "id": "BUG-014",
    "category": "边界条件",
    "difficulty": "simple",
    "issue": "`chunk`(src/slicing.py)按每批 n 条切分序列;当前每批多出一条(n+1 条)。请修复。",
    "module": "src/slicing.py",
    "buggy_code": '''def chunk(items, n):
    """把序列切成每批最多 n 条。"""
    if n < 1:
        raise ValueError("n must be >= 1")
    return [items[i:i + n + 1] for i in range(0, len(items), n)]
''',
    "fixed_code": '''def chunk(items, n):
    """把序列切成每批最多 n 条。"""
    if n < 1:
        raise ValueError("n must be >= 1")
    return [items[i:i + n] for i in range(0, len(items), n)]
''',
    "test_path": "tests/test_slicing.py",
    "test_code": """import pytest

from src.slicing import chunk


def test_even_split():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_remainder_chunk():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_invalid_n_raises():
    with pytest.raises(ValueError):
        chunk([1], 0)


def test_empty_input():
    assert chunk([], 3) == []
""",
    "failed": ["test_even_split", "test_remainder_chunk"],
    "regression": ["test_invalid_n_raises", "test_empty_input"],
    "allowed_paths": ["src/**"],
    "search_hint": "def chunk",
}

BUG_015 = {
    "id": "BUG-015",
    "category": "异常处理",
    "difficulty": "simple",
    "issue": "`read_head`(src/fileio.py)读取文件前 n 行,文件不存在时契约返回空列表;当前抛 FileNotFoundError。请修复。",
    "module": "src/fileio.py",
    "buggy_code": '''from pathlib import Path


def read_head(path, n):
    """返回文件前 n 行(不含换行符);文件不存在返回 []。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines[:n]
''',
    "fixed_code": '''from pathlib import Path


def read_head(path, n):
    """返回文件前 n 行(不含换行符);文件不存在返回 []。"""
    if not Path(path).exists():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines[:n]
''',
    "test_path": "tests/test_fileio.py",
    "test_code": """from src.fileio import read_head


def test_reads_first_lines(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("l1\\nl2\\nl3\\nl4", encoding="utf-8")
    assert read_head(f, 2) == ["l1", "l2"]


def test_missing_file_returns_empty(tmp_path):
    assert read_head(tmp_path / "nope.txt", 3) == []


def test_n_larger_than_file(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("a", encoding="utf-8")
    assert read_head(f, 10) == ["a"]
""",
    "failed": ["test_missing_file_returns_empty"],
    "regression": ["test_reads_first_lines", "test_n_larger_than_file"],
    "allowed_paths": ["src/**"],
    "search_hint": "read_head",
}

BUG_016 = {
    "id": "BUG-016",
    "category": "数据访问",
    "difficulty": "medium",
    "issue": "`index_of`(src/tabular.py)在行集合中查找某列等于指定值的行号;缺少该列的行应跳过,当前抛 KeyError。请修复。",
    "module": "src/tabular.py",
    "buggy_code": '''def index_of(rows, column, value):
    """返回第一个 rows[i][column] == value 的下标;找不到返回 -1;缺列的行跳过。"""
    for i, row in enumerate(rows):
        if row[column] == value:
            return i
    return -1
''',
    "fixed_code": '''def index_of(rows, column, value):
    """返回第一个 rows[i][column] == value 的下标;找不到返回 -1;缺列的行跳过。"""
    for i, row in enumerate(rows):
        if column not in row:
            continue
        if row[column] == value:
            return i
    return -1
''',
    "test_path": "tests/test_tabular.py",
    "test_code": """from src.tabular import index_of


def test_found_returns_index():
    rows = [{"id": "a"}, {"id": "b"}]
    assert index_of(rows, "id", "b") == 1


def test_missing_column_is_skipped():
    rows = [{"other": 1}, {"id": "x"}]
    assert index_of(rows, "id", "x") == 1


def test_not_found_returns_minus_one():
    assert index_of([{"id": "a"}], "id", "z") == -1


def test_empty_rows():
    assert index_of([], "id", "a") == -1
""",
    "failed": ["test_missing_column_is_skipped"],
    "regression": [
        "test_found_returns_index",
        "test_not_found_returns_minus_one",
        "test_empty_rows",
    ],
    "allowed_paths": ["src/**"],
    "search_hint": "index_of",
}

BUG_017 = {
    "id": "BUG-017",
    "category": "类型错误",
    "difficulty": "simple",
    "issue": "`average`(src/stats.py)对整数输入返回了截断的整数结果,契约要求浮点平均。请修复。",
    "module": "src/stats.py",
    "buggy_code": '''def average(numbers):
    """算术平均;空序列抛 ValueError。"""
    if not numbers:
        raise ValueError("empty sequence")
    return sum(numbers) // len(numbers)
''',
    "fixed_code": '''def average(numbers):
    """算术平均;空序列抛 ValueError。"""
    if not numbers:
        raise ValueError("empty sequence")
    return sum(numbers) / len(numbers)
''',
    "test_path": "tests/test_stats.py",
    "test_code": """import pytest

from src.stats import average


def test_fractional_average():
    assert average([1, 2]) == 1.5


def test_integer_average():
    assert average([2, 4]) == 3.0


def test_empty_raises():
    with pytest.raises(ValueError):
        average([])
""",
    "failed": ["test_fractional_average"],
    "regression": ["test_integer_average", "test_empty_raises"],
    "allowed_paths": ["src/**"],
    "search_hint": "def average",
}

BUG_018 = {
    "id": "BUG-018",
    "category": "边界条件",
    "difficulty": "simple",
    "issue": "`is_leap`(src/calendar_ops.py)闰年判断遗漏了整百年规则:能被 100 整除但不能被 400 整除的不是闰年。请修复。",
    "module": "src/calendar_ops.py",
    "buggy_code": '''def is_leap(year):
    """闰年:能被 4 整除;整百年须能被 400 整除。"""
    return year % 4 == 0
''',
    "fixed_code": '''def is_leap(year):
    """闰年:能被 4 整除;整百年须能被 400 整除。"""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
''',
    "test_path": "tests/test_calendar_ops.py",
    "test_code": """from src.calendar_ops import is_leap


def test_regular_leap_year():
    assert is_leap(2024) is True


def test_century_non_leap():
    assert is_leap(1900) is False


def test_400_year_is_leap():
    assert is_leap(2000) is True


def test_common_year():
    assert is_leap(2023) is False
""",
    "failed": ["test_century_non_leap"],
    "regression": ["test_regular_leap_year", "test_common_year", "test_400_year_is_leap"],
    "allowed_paths": ["src/**"],
    "search_hint": "is_leap",
}

BUG_019 = {
    "id": "BUG-019",
    "category": "异常处理",
    "difficulty": "simple",
    "issue": "`first_or`(src/seq.py)在空序列时应返回 default,当前抛 IndexError。请修复。",
    "module": "src/seq.py",
    "buggy_code": '''def first_or(items, default=None):
    """返回第一个元素;空序列返回 default。"""
    return items[0]
''',
    "fixed_code": '''def first_or(items, default=None):
    """返回第一个元素;空序列返回 default。"""
    if not items:
        return default
    return items[0]
''',
    "test_path": "tests/test_seq_first.py",
    "test_code": """from src.seq import first_or


def test_returns_first():
    assert first_or([3, 1], default=0) == 3


def test_empty_returns_default():
    assert first_or([], default="none") == "none"


def test_default_is_none():
    assert first_or([]) is None
""",
    "failed": ["test_empty_returns_default", "test_default_is_none"],
    "regression": ["test_returns_first"],
    "allowed_paths": ["src/**"],
    "search_hint": "first_or",
}

BUG_020 = {
    "id": "BUG-020",
    "category": "跨文件定位",
    "difficulty": "medium",
    "issue": (
        "购物车结算函数 `cart_total`(src/cart.py)对满 100 元的商品没有给出 9 折优惠。"
        "症状在 cart 的合计,但折扣规则在它依赖的模块里。请定位根因并修复,保证原有测试通过。"
    ),
    "module": "src/pricing.py",
    "extra_files": {
        "src/cart.py": '''"""购物车结算。"""

from src.pricing import unit_price


def cart_total(items):
    """合计:每件商品按数量计价,单价内部应用折扣。"""
    return round(sum(unit_price(name) * qty for name, qty in items), 2)
''',
    },
    "buggy_code": '''"""定价规则。"""

PRICE_LIST = {"pen": 5.0, "book": 60.0, "lamp": 120.0, "desk": 100.0}
BULK_THRESHOLD = 100
BULK_DISCOUNT = 0.9


def unit_price(name):
    """单价:原价超过 BULK_THRESHOLD 打 BULK_DISCOUNT 折。"""
    price = PRICE_LIST[name]
    if price > BULK_THRESHOLD:
        return price * BULK_DISCOUNT
    return price
''',
    "fixed_code": '''"""定价规则。"""

PRICE_LIST = {"pen": 5.0, "book": 60.0, "lamp": 120.0, "desk": 100.0}
BULK_THRESHOLD = 100
BULK_DISCOUNT = 0.9


def unit_price(name):
    """单价:原价达到 BULK_THRESHOLD 打 BULK_DISCOUNT 折。"""
    price = PRICE_LIST[name]
    if price >= BULK_THRESHOLD:
        return price * BULK_DISCOUNT
    return price
''',
    "test_path": "tests/test_cart.py",
    "test_code": """from src.cart import cart_total


def test_no_discount_items():
    assert cart_total([("pen", 2)]) == 10.0


def test_discount_applies_at_threshold():
    # desk 原价恰好 100,满 100 打 9 折 = 90
    assert cart_total([("desk", 1)]) == 90.0


def test_mixed_items():
    # book 60 不打折,lamp 120 打 9 折 108
    assert cart_total([("book", 1), ("lamp", 1)]) == 168.0
""",
    "failed": ["test_discount_applies_at_threshold"],
    "regression": ["test_no_discount_items", "test_mixed_items"],
    "allowed_paths": ["src/pricing.py", "src/cart.py"],
    "search_hint": "unit_price",
}

SPECS = [
    BUG_001,
    BUG_002,
    BUG_003,
    BUG_004,
    BUG_005,
    BUG_006,
    BUG_007,
    BUG_008,
    BUG_009,
    BUG_010,
    BUG_011,
    BUG_012,
    BUG_013,
    BUG_014,
    BUG_015,
    BUG_016,
    BUG_017,
    BUG_018,
    BUG_019,
    BUG_020,
]


# ---------------------------------------------------------------------------
# 物化
# ---------------------------------------------------------------------------


def unified_diff(module_path: str, buggy: str, fixed: str) -> str:
    return "".join(
        difflib.unified_diff(
            buggy.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"a/{module_path}",
            tofile=f"b/{module_path}",
        )
    )


def replay_script(spec: dict, diff_text: str) -> list[dict]:
    """plain 引擎回放脚本:单个循环走完全部步骤。"""
    return [
        {"tool": "search_code", "args": {"keyword": spec["search_hint"]}},
        {"tool": "read_file", "args": {"path": spec["module"]}},
        {"tool": "apply_patch", "args": {"diff_text": diff_text}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {
            "tool": "finish",
            "args": {"success": True, "summary": f"修复 {spec['module']} 并通过全部测试"},
        },
    ]


def graph_replay_script(spec: dict, diff_text: str) -> list[dict]:
    """graph 引擎回放脚本:定位(只读)与补丁(写工具)两段拼接。

    两阶段共用一个 FakeLLM 时按顺序消费;定位段不能出现写工具,
    否则会在 LOCALIZE 阶段被阶段限制拦截、脚本错位。
    """
    localize = [
        {"tool": "search_code", "args": {"keyword": spec["search_hint"]}},
        {"tool": "read_file", "args": {"path": spec["module"]}},
        {"tool": "finish", "args": {"success": True, "summary": f"根因定位:{spec['module']}"}},
    ]
    propose = [
        {"tool": "apply_patch", "args": {"diff_text": diff_text}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {
            "tool": "finish",
            "args": {"success": True, "summary": f"修复 {spec['module']} 并通过全部测试"},
        },
    ]
    return localize + propose


def gen_bug(spec: dict) -> Path:
    bug_dir = BUGS / spec["id"]
    (bug_dir / "repo" / "src").mkdir(parents=True, exist_ok=True)
    (bug_dir / "repo" / "tests").mkdir(parents=True, exist_ok=True)
    (bug_dir / "replay").mkdir(parents=True, exist_ok=True)

    repo = bug_dir / "repo"
    (repo / "conftest.py").write_text(_conftest(), encoding="utf-8", newline="\n")
    (repo / spec["module"]).write_text(spec["buggy_code"], encoding="utf-8", newline="\n")
    for rel, content in spec.get("extra_files", {}).items():
        (repo / rel).write_text(content, encoding="utf-8", newline="\n")
    (repo / spec["test_path"]).write_text(spec["test_code"], encoding="utf-8", newline="\n")

    (bug_dir / "issue.md").write_text(spec["issue"] + "\n", encoding="utf-8", newline="\n")

    failed = [f"{spec['test_path']}::{name}" for name in spec["failed"]]
    regression = [f"{spec['test_path']}::{name}" for name in spec["regression"]]
    manifest = (
        f"id: {spec['id']}\n"
        f"category: {spec['category']}\n"
        f"difficulty: {spec['difficulty']}\n"
        'test_cmd: "{python} -m pytest"\n'
        "failed_tests:\n"
        + "".join(f"  - {t}\n" for t in failed)
        + "regression_tests:\n"
        + "".join(f"  - {t}\n" for t in regression)
        + "allowed_paths:\n"
        + "".join(f"  - {p}\n" for p in spec["allowed_paths"])
        + "max_rounds: 5\n"
    )
    (bug_dir / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")

    diff_text = unified_diff(spec["module"], spec["buggy_code"], spec["fixed_code"])
    (bug_dir / "expected" / "reference.diff").parent.mkdir(parents=True, exist_ok=True)
    (bug_dir / "expected" / "reference.diff").write_text(diff_text, encoding="utf-8", newline="\n")
    script = replay_script(spec, diff_text)
    (bug_dir / "replay" / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    (bug_dir / "replay" / "graph-script.json").write_text(
        json.dumps(graph_replay_script(spec, diff_text), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"[gen] {spec['id']} ({spec['category']}/{spec['difficulty']})")
    return bug_dir


def validate_baseline(bug_dir: Path, python_exe: str) -> bool:
    """基线校验:failed 测试必须失败,regression 测试必须通过。"""
    import shutil

    from app.evals.bugset import load_bug

    tmp = Path(tempfile.mkdtemp(prefix="bugcheck-"))
    try:
        bug = load_bug(bug_dir)
        work = tmp / "ws"
        from app.gitops.testing import materialize_repo

        materialize_repo(bug.repo_dir, work, extra_commit=False)
        for ids, expect_pass in ((bug.failed_tests, False), (bug.regression_tests, True)):
            proc = subprocess.run(
                [
                    python_exe,
                    "-m",
                    "pytest",
                    "-q",
                    "--color=no",
                    f"--junitxml={tmp / 'j.xml'}",
                    f"--basetemp={tmp / 'basetemp'}",
                    *ids,
                ],
                cwd=work,
                capture_output=True,
                timeout=120,
            )
            ok = proc.returncode == 0
            if ok != expect_pass:
                print(
                    f"[validate] {bug.id}: {'regression' if expect_pass else 'failed'} set "
                    f"unexpected rc={proc.returncode}"
                )
                return False
        print(f"[validate] {bug.id} baseline OK")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    if "--validate" in argv:
        argv.remove("--validate")
        do_validate = True
    else:
        do_validate = False

    targets = [s for s in SPECS if not argv or s["id"] in argv]
    if not targets:
        print("no spec matched:", argv)
        return 1
    for spec in targets:
        gen_bug(spec)

    if do_validate:
        python_exe = sys.executable
        results = [(s["id"], validate_baseline(BUGS / s["id"], python_exe)) for s in targets]
        return 0 if all(ok for _, ok in results) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
