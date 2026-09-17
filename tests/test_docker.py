"""M8 Docker 执行器测试:真实容器内执行 pytest 并解析报告(守护进程不可用时跳过)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.executor.docker_runner import docker_available, run_tests_in_container
from app.gitops.testing import materialize_repo

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker 守护进程不可用")

BUG_ROOT = Path("bugs")


def test_container_pytest_baseline_and_pass(tmp_path: Path) -> None:
    """同一镜像内:基线失败集报 1 failed;修复后全部通过(完整容器内 VERIFY)。"""
    work = tmp_path / "ws"
    materialize_repo(BUG_ROOT / "BUG-003" / "repo", work, extra_commit=False)
    reports = tmp_path / "reports"
    failed_ids = ["tests/test_labels.py::test_default_separator"]
    keep_ids = ["tests/test_labels.py::test_custom_separator"]

    report, run = run_tests_in_container(
        work, failed_ids, image="patchpilot-executor:latest", report_dir=reports
    )
    assert not report.all_passed
    assert report.failed == 1
    assert report.failed_cases[0].test_name == "test_default_separator"
    assert not run.timed_out

    # 应用正确修复(工作区内直接改,模拟补丁已应用)
    labels = work / "src" / "labels.py"
    labels.write_text(
        '"""标签拼接工具。"""\n\n\ndef join_labels(labels, sep=None):\n'
        '    separator = "," if sep is None else sep\n'
        '    result = ""\n'
        "    for index, label in enumerate(labels):\n"
        "        if index > 0:\n"
        "            result += separator\n"
        "        result += label\n"
        "    return result\n",
        encoding="utf-8",
        newline="\n",
    )
    report2, _ = run_tests_in_container(
        work, failed_ids + keep_ids, image="patchpilot-executor:latest", report_dir=reports
    )
    assert report2.all_passed and report2.passed == 2
    # junit 报告落在宿主的 reports 目录,不污染工作区
    assert len(list(reports.glob("junit-*.xml"))) == 2


def test_run_pytest_docker_backend_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """N6 接线验收:backend=docker 时 run_pytest 全链路容器化——
    基线失败集在容器内报失败,修复后在容器内转通过,junit 仍回传宿主。"""
    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    monkeypatch.setenv("PATCHPILOT_DOCKER_IMAGE", "patchpilot-executor:latest")
    from app.adapters.pytest_adapter import run_pytest
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        work = tmp_path / "ws"
        materialize_repo(BUG_ROOT / "BUG-003" / "repo", work, extra_commit=False)
        reports = tmp_path / "reports"
        failed_ids = ["tests/test_labels.py::test_default_separator"]
        keep_ids = ["tests/test_labels.py::test_custom_separator"]

        report, run = run_pytest(
            "python", work, failed_ids, report_path=reports / "baseline-junit.xml"
        )
        assert not report.all_passed and report.failed == 1
        assert not run.timed_out

        labels = work / "src" / "labels.py"
        labels.write_text(
            '"""标签拼接工具。"""\n\n\ndef join_labels(labels, sep=None):\n'
            '    separator = "," if sep is None else sep\n'
            '    result = ""\n'
            "    for index, label in enumerate(labels):\n"
            "        if index > 0:\n            result += separator\n        result += label\n"
            "    return result\n",
            encoding="utf-8",
            newline="\n",
        )
        report2, _ = run_pytest(
            "python", work, failed_ids + keep_ids, report_path=reports / "verify-junit.xml"
        )
        assert report2.all_passed and report2.passed == 2
        assert len(list(reports.glob("junit-*.xml"))) == 2  # junit 回传宿主,不污染工作区
    finally:
        get_settings.cache_clear()
