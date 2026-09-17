"""N6:执行后端分发骨架 —— local 直跑,docker 路径只做命令拼装(monkeypatch,不起真容器)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import ExecError
from app.executor.backend import build_docker_command, run_tests_by_backend
from app.executor.local_runner import TestRunResult


def test_settings_rejects_unknown_backend() -> None:
    """非法值在 Settings 构造(即启动)时立刻报错。"""
    with pytest.raises(ValidationError) as exc_info:
        Settings(execution_backend="k8s")
    assert "execution_backend" in str(exc_info.value)


def test_settings_accepts_local_and_docker() -> None:
    assert Settings(execution_backend="local").execution_backend == "local"
    assert Settings(execution_backend="docker").execution_backend == "docker"


def test_local_backend_runs_real_command(tmp_path) -> None:
    result = run_tests_by_backend(
        [sys.executable, "-c", "print('backend-ok')"], tmp_path, timeout_seconds=30
    )
    assert result.exit_code == 0
    assert "backend-ok" in result.stdout_tail
    assert result.timed_out is False


def test_docker_backend_wraps_command(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """docker 路径拼装隔离命令并交由 run_tests 执行(捕获,不真跑容器)。"""
    captured: dict = {}

    def fake_run_tests(command, cwd, timeout_seconds=None):
        captured["command"] = command
        captured["cwd"] = cwd
        return TestRunResult(
            command=command, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
        )

    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    monkeypatch.setenv("PATCHPILOT_DOCKER_IMAGE", "pytest-image:latest")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.backend.run_tests", fake_run_tests)
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: True)
    try:
        original = [sys.executable, "-m", "pytest", "-q", "tests/test_x.py"]
        result = run_tests_by_backend(original, tmp_path, timeout_seconds=60)
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0
    assert captured["cwd"] == tmp_path
    command = captured["command"]
    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert "--memory" in command and "--cpus" in command
    assert "pytest-image:latest" in command
    ws = str(tmp_path.resolve())
    assert f"{ws}:{ws}" in command  # 工作区挂载
    assert command[-len(original) :] == original  # 原命令在尾部完整展开


def test_docker_backend_unavailable_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: False)
    try:
        with pytest.raises(ExecError) as exc_info:
            run_tests_by_backend([sys.executable, "-c", "pass"], tmp_path)
    finally:
        get_settings.cache_clear()
    assert "not available" in str(exc_info.value)


def test_empty_command_rejected(tmp_path) -> None:
    with pytest.raises(ExecError):
        run_tests_by_backend([], tmp_path)


def test_build_docker_command_shape(tmp_path) -> None:
    command = build_docker_command(["python", "-m", "pytest"], image="img:1", workspace=tmp_path)
    ws = str(tmp_path.resolve())
    assert command == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "-v",
        f"{ws}:{ws}",
        "-w",
        ws,
        "img:1",
        "python",
        "-m",
        "pytest",
    ]


def test_run_pytest_routes_to_container_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """backend=docker 时 run_pytest 走容器执行器,返回结构与 local 完全一致。"""
    from app.adapters.pytest_adapter import PytestReport, run_pytest

    captured: dict = {}
    fake_report = PytestReport(exit_code=0, passed=2)
    fake_run = TestRunResult(
        command=["docker", "run"], exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
    )

    def fake_container(workspace, test_ids, *, report_dir, timeout_seconds=None, **kwargs):
        captured.update(workspace=workspace, test_ids=list(test_ids or []), report_dir=report_dir)
        return fake_report, fake_run

    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.docker_runner.run_tests_in_container", fake_container)
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: True)
    reports = tmp_path.parent / "reports"
    try:
        report, run = run_pytest(
            sys.executable,
            tmp_path,
            ["tests/test_a.py::test_one"],
            report_path=reports / "junit.xml",
        )
    finally:
        get_settings.cache_clear()

    assert report is fake_report and run is fake_run
    assert captured["workspace"] == tmp_path
    assert captured["test_ids"] == ["tests/test_a.py::test_one"]
    assert captured["report_dir"] == reports


def test_run_pytest_docker_unavailable_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.adapters.pytest_adapter import run_pytest
    from app.errors import ExecError

    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: False)
    try:
        with pytest.raises(ExecError) as exc_info:
            run_pytest(sys.executable, tmp_path)
    finally:
        get_settings.cache_clear()
    assert "not available" in str(exc_info.value)
