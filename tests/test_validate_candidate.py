"""P3-14:候选题校验脚本(pretrflight)逐题原始输出落盘机制。

背景:2026-09-25 夜间 E5 转正的 7 道 preflight 三件套只留下"通过"的结论,
逐题原始输出不存在(审计 R1-Q2-5:证据与结论没有区分)。--log-dir 落盘后,
审题时可逐题核对基线断言的原始 pytest 输出。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_candidate import main, validate

_MANIFEST = """\
id: BUG-C900
category: 边界条件
difficulty: hard
test_cmd: "{{python}} -m pytest"
failed_tests:
  - tests/test_dateparse.py::test_empty_string_returns_none
regression_tests:
  - tests/test_dateparse.py::test_iso_format
  - tests/test_dateparse.py::test_none_returns_none
allowed_paths:
  - src/dateparse.py
max_rounds: 5
"""

# 与 tests/test_service_robustness.py 的 tiny fixture 同构:空白串缺陷
_BROKEN_SRC = '''"""日期解析工具。"""

from datetime import datetime

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
'''

_TESTS = """import pytest
from src.dateparse import parse_date


def test_iso_format():
    assert parse_date("2026-01-31").isoformat() == "2026-01-31"


def test_none_returns_none():
    assert parse_date(None) is None


def test_empty_string_returns_none():
    assert parse_date("") is None
"""

_REPLAY = [{"tool": "finish", "args": {"success": True, "summary": "noop"}}]


def _make_candidate(root: Path) -> Path:
    bug = root / "BUG-C900"
    (bug / "repo" / "src").mkdir(parents=True)
    (bug / "repo" / "tests").mkdir(parents=True)
    (bug / "repo" / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (bug / "repo" / "src" / "dateparse.py").write_text(_BROKEN_SRC, encoding="utf-8", newline="\n")
    (bug / "repo" / "tests" / "test_dateparse.py").write_text(
        _TESTS, encoding="utf-8", newline="\n"
    )
    (bug / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8", newline="\n")
    (bug / "issue.md").write_text("空字符串输入未被处理为 None。", encoding="utf-8", newline="\n")
    (bug / "replay").mkdir()
    for name in ("script.json", "graph-script.json"):
        (bug / "replay" / name).write_text(
            json.dumps(_REPLAY, ensure_ascii=False), encoding="utf-8", newline="\n"
        )
    return bug


def test_validate_passes_and_logs_raw_outputs(tmp_path: Path) -> None:
    """基线断言全过时,每次 pytest 的原始输出逐题落盘(命令/退出码/stdout)。"""
    bug = _make_candidate(tmp_path)
    log_dir = tmp_path / "preflight"

    problems = validate(bug, log_dir)

    assert problems == []
    files = sorted((log_dir / "BUG-C900").glob("*.txt"))
    assert files, "逐题原始输出未落盘"
    assert len(files) == 2  # 1 条 failed 逐条跑(1 次)+ 1 次回归整体跑
    text = files[0].read_text(encoding="utf-8")
    assert "exit_code:" in text
    assert "--- stdout ---" in text
    assert "-m pytest" in text  # 可复核的命令行


def test_validate_failure_still_logs_evidence(tmp_path: Path) -> None:
    """基线断言不过(把缺陷修好 → failed_test 意外通过)时,结论与证据同在。"""
    bug = _make_candidate(tmp_path)
    fixed = bug / "repo" / "src" / "dateparse.py"
    fixed.write_text(
        _BROKEN_SRC.replace(
            "    if value is None:\n        return None\n",
            "    if value is None or not value.strip():\n        return None\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    log_dir = tmp_path / "preflight"

    problems = validate(bug, log_dir)

    assert any("unexpectedly passes at baseline" in p for p in problems)
    files = list((log_dir / "BUG-C900").glob("*.txt"))
    assert files, "失败结论也必须留下原始输出"
    assert any("exit_code: 0" in f.read_text(encoding="utf-8") for f in files)


def test_main_accepts_log_dir_flag(tmp_path: Path, capsys) -> None:
    """CLI 入口:--log-dir 落盘并在结论行提示证据位置。"""
    bug = _make_candidate(tmp_path)
    rc = main([str(bug), "--log-dir", str(tmp_path / "pf")])
    assert rc == 0
    assert (tmp_path / "pf" / "BUG-C900").is_dir()
    out = capsys.readouterr().out
    assert "raw outputs" in out
