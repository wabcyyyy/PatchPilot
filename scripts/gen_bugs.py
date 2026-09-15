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

SPECS = [BUG_001, BUG_002, BUG_003, BUG_004, BUG_005]


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
        return 0 if all(validate_baseline(BUGS / s["id"], python_exe) for s in targets) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
