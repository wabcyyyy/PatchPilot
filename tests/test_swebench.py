"""T10.3 SWE-bench 适配器测试:数据解析、映射与离线 e2e(fake 回放)。"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

from app.errors import TaskError
from app.evals.driver import run_task
from app.evals.swebench import load_instances, parse_instance, to_bug_task
from app.llm.fake import FakeLLM

FIXTURE = Path("tests/fixtures/swebench_sample.jsonl")

BUGGY = '''"""安全算术。"""


def safe_divide(a, b):
    """安全除法:除数为 0 返回 None。"""
    return a / b
'''
FIXED = '''"""安全算术。"""


def safe_divide(a, b):
    """安全除法:除数为 0 返回 None。"""
    if b == 0:
        return None
    return a / b
'''
TESTS = """from src.arith import safe_divide


def test_normal_division():
    assert safe_divide(6, 3) == 2.0


def test_zero_divisor_returns_none():
    assert safe_divide(1, 0) is None
"""


def test_parse_instance_accepts_json_string_and_list() -> None:
    from_str = parse_instance(
        {
            "instance_id": "x-1",
            "repo": "demo/arith",
            "base_commit": "abc",
            "problem_statement": "p",
            "FAIL_TO_PASS": '["t::a"]',
            "PASS_TO_PASS": '["t::b", "t::c"]',
        }
    )
    assert from_str.fail_to_pass == ["t::a"] and from_str.pass_to_pass == ["t::b", "t::c"]

    from_list = parse_instance(
        {
            "instance_id": "x-2",
            "FAIL_TO_PASS": ["t::d"],
            "PASS_TO_PASS": [],
            "problem_statement": "p",
        }
    )
    assert from_list.fail_to_pass == ["t::d"]


def test_empty_fail_to_pass_rejected() -> None:
    with pytest.raises(TaskError, match="FAIL_TO_PASS"):
        parse_instance({"instance_id": "x-3", "FAIL_TO_PASS": "[]"})


def test_load_instances_from_fixture() -> None:
    instances = load_instances(FIXTURE)
    assert [i.instance_id for i in instances] == ["demo-001", "demo-002"]
    assert instances[0].fail_to_pass == ["tests/test_arith.py::test_zero_divisor_returns_none"]
    assert instances[0].base_commit.startswith("a1b2c3d4")


def test_load_instances_reports_bad_lines() -> None:
    bad = Path(__file__).parent / "_bad.jsonl"
    ok_line = {"instance_id": "ok", "FAIL_TO_PASS": '["t"]', "PASS_TO_PASS": "[]"}
    bad.write_text(json.dumps(ok_line) + "\n{not json}\n", encoding="utf-8")
    try:
        with pytest.raises(TaskError, match="bad.jsonl:2"):
            load_instances(bad)
    finally:
        bad.unlink()


def test_to_bug_task_requires_local_checkout(tmp_path: Path) -> None:
    (instance,) = [i for i in load_instances(FIXTURE) if i.instance_id == "demo-001"]
    with pytest.raises(TaskError, match="local checkout"):
        to_bug_task(instance, tmp_path / "nope")


def test_swebench_task_resolves_via_replay(tmp_path: Path) -> None:
    """全离线 e2e:本地 checkout → BugTask → 平台驱动器 fake 回放 resolved。"""
    checkout = tmp_path / "demo-001"
    (checkout / "src").mkdir(parents=True)
    (checkout / "tests").mkdir()
    # newline="\n":Windows 下默认会把 \n 翻译成 CRLF,git apply 会因行尾不匹配拒 patch
    (checkout / "conftest.py").write_text('"""repo conftest."""\n', encoding="utf-8", newline="\n")
    (checkout / "src" / "arith.py").write_text(BUGGY, encoding="utf-8", newline="\n")
    (checkout / "tests" / "test_arith.py").write_text(TESTS, encoding="utf-8", newline="\n")

    (instance,) = [i for i in load_instances(FIXTURE) if i.instance_id == "demo-001"]
    task = to_bug_task(instance, checkout)
    assert task.category == "swe-bench" and task.difficulty == "external"

    diff = "".join(
        difflib.unified_diff(
            BUGGY.splitlines(keepends=True),
            FIXED.splitlines(keepends=True),
            fromfile="a/src/arith.py",
            tofile="b/src/arith.py",
        )
    )
    script = [
        {"tool": "search_code", "args": {"keyword": "safe_divide"}},
        {"tool": "read_file", "args": {"path": "src/arith.py"}},
        {"tool": "apply_patch", "args": {"diff_text": diff}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "fixed"}},
    ]
    result = run_task(task, FakeLLM(script), runs_root=tmp_path / "runs")
    assert result.verdict == "resolved" and result.status == "FINISHED"
    assert result.changed_files == ["src/arith.py"]


def test_import_script_reports_missing_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from scripts.import_swebench import main as import_main

    rc = import_main(["--jsonl", str(FIXTURE), "--checkouts-root", str(tmp_path)])
    assert rc == 1
    assert "[missing]" in capsys.readouterr().out


def test_to_bug_task_rejects_injection_test_ids(tmp_path: Path) -> None:
    """N-4 整改:jsonl 是外部数据,FAIL_TO_PASS/PASS_TO_PASS 必须过与 manifest 相同的校验。"""
    checkout = tmp_path / "evil-001"
    checkout.mkdir()
    instance = parse_instance(
        {
            "instance_id": "evil-001",
            "FAIL_TO_PASS": ["-p", "evil_plugin"],
            "PASS_TO_PASS": ["//host/share/test_x.py::t"],
            "problem_statement": "p",
        }
    )
    with pytest.raises(TaskError, match="evil-001"):
        to_bug_task(instance, checkout)
