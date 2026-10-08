"""S06:受理预检(app/api/preflight.py)——依赖/收集问题与业务断言失败分类。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.api.preflight import run_preflight
from app.errors import InvalidRequestError


def _repo(root: Path, *, test_body: str = "def test_ok():\n    assert True\n") -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (root / "tests" / "test_x.py").write_text(test_body, encoding="utf-8", newline="\n")
    return root


def _ids() -> list[str]:
    return ["tests/test_x.py::test_ok"]


def test_preflight_ok_records_duration(tmp_path: Path) -> None:
    ws = _repo(tmp_path / "ok")
    report = run_preflight(
        python_exe=sys.executable,
        workspace=ws,
        failed_tests=_ids(),
        regression_tests=_ids(),
        timeout_seconds=60,
    )
    assert report.ok is True
    assert report.duration_ms >= 0
    assert {c.name for c in report.checks} >= {
        "interpreter",
        "pytest",
        "collect_failed_set",
        "collect_regression_set",
    }


def test_preflight_missing_interpreter_fails_without_pytest_probe(tmp_path: Path) -> None:
    ws = _repo(tmp_path / "ws")
    with pytest.raises(InvalidRequestError, match="interpreter"):
        run_preflight(
            python_exe=str(tmp_path / "no-such-python.exe"),
            workspace=ws,
            failed_tests=_ids(),
            regression_tests=_ids(),
            timeout_seconds=60,
        )


def test_preflight_classifies_missing_dependency_as_collection_error(tmp_path: Path) -> None:
    """依赖缺失 = 环境问题:收集错误,不是断言失败(分类消息必须点名这一点)。"""
    ws = _repo(tmp_path / "dep")
    (ws / "tests" / "test_x.py").write_text(
        "import not_installed_anywhere_module_xyz\n\ndef test_ok():\n    assert True\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(InvalidRequestError) as exc:
        run_preflight(
            python_exe=sys.executable,
            workspace=ws,
            failed_tests=_ids(),
            regression_tests=_ids(),
            timeout_seconds=60,
        )
    assert "collection error" in str(exc.value)
    assert "environment problem" in str(exc.value)


def test_preflight_detects_bad_test_target_as_usage_error(tmp_path: Path) -> None:
    ws = _repo(tmp_path / "bad")
    with pytest.raises(InvalidRequestError, match="usage error"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws,
            failed_tests=["tests/test_x.py::test_does_not_exist"],
            regression_tests=_ids(),
            timeout_seconds=60,
        )


def test_preflight_detects_non_test_target(tmp_path: Path) -> None:
    """请求的不是可收集用例(rc=4 无 ERROR)= 目标问题,与环境问题分类不同。"""
    ws = _repo(tmp_path / "empty", test_body="def not_a_test():\n    pass\n")
    with pytest.raises(InvalidRequestError, match="usage error"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws,
            failed_tests=["tests/test_x.py::not_a_test"],
            regression_tests=_ids(),
            timeout_seconds=60,
        )


def test_preflight_business_assertion_failure_is_not_preflights_business(tmp_path: Path) -> None:
    """断言失败不影响收集:预检通过——红/绿判定属于引擎的 baseline,不归预检。"""
    ws = _repo(tmp_path / "red", test_body="def test_ok():\n    assert False\n")
    report = run_preflight(
        python_exe=sys.executable,
        workspace=ws,
        failed_tests=_ids(),
        regression_tests=_ids(),
        timeout_seconds=60,
    )
    assert report.ok is True
