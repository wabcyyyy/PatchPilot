"""执行后端:Settings 校验 + run_pytest 按 execution_backend 路由(docker 路径全 monkeypatch,不起真容器)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.executor.local_runner import TestRunResult


def test_settings_rejects_unknown_backend() -> None:
    """非法值在 Settings 构造(即启动)时立刻报错。"""
    with pytest.raises(ValidationError) as exc_info:
        Settings(execution_backend="k8s")
    assert "execution_backend" in str(exc_info.value)


def test_settings_accepts_local_and_docker() -> None:
    assert Settings(execution_backend="local").execution_backend == "local"
    assert Settings(execution_backend="docker").execution_backend == "docker"


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


def test_run_pytest_threads_bug_env_into_container(monkeypatch, tmp_path: Path) -> None:
    """题目自带容器环境时,image/workdir/python 必须逐条下发到容器执行器。

    挂载点是这里的要害:镜像把仓库预装在 /testbed 并按该路径做了 editable 安装,
    工作区若仍挂在默认的 /ws,import 到的是镜像里未修改的代码,基线会假绿。
    """
    from app.adapters.pytest_adapter import BugEnv, PytestReport, run_pytest

    captured: dict = {}
    fake_report = PytestReport(exit_code=1, failed=1)
    fake_run = TestRunResult(
        command=["docker", "run"], exit_code=1, stdout_tail="", stderr_tail="", duration_ms=1
    )

    def fake_container(workspace, test_ids, *, report_dir, timeout_seconds=None, **kwargs):
        captured.update(kwargs)
        return fake_report, fake_run

    env = BugEnv(
        python="/opt/miniconda3/envs/testbed/bin/python",
        image="swebench/sweb.eval.x86_64.demo:latest",
        workdir="/testbed",
    )
    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.docker_runner.run_tests_in_container", fake_container)
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: True)
    try:
        report, _ = run_pytest(
            sys.executable,
            tmp_path,
            ["tests/test_a.py::test_one"],
            report_path=tmp_path / "junit.xml",
            env=env,
        )
    finally:
        get_settings.cache_clear()

    assert report is fake_report
    assert captured == {
        "image": "swebench/sweb.eval.x86_64.demo:latest",
        "workdir": "/testbed",
        "python_bin": "/opt/miniconda3/envs/testbed/bin/python",
    }


def test_env_network_defaults_to_none_and_forwards_when_declared(
    monkeypatch, tmp_path: Path
) -> None:
    """网络边界默认不动(不下发 network 参数,沿用 docker_runner 的 none);
    只有题目 manifest 显式声明才逐题下发,并且只允许枚举内的值。
    """
    from app.adapters.pytest_adapter import BugEnv, PytestReport, run_pytest

    captured: dict = {}
    fake_report = PytestReport(exit_code=0, passed=1)
    fake_run = TestRunResult(
        command=["docker", "run"], exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
    )

    def fake_container(workspace, test_ids, *, report_dir, timeout_seconds=None, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return fake_report, fake_run

    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "docker")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.executor.docker_runner.run_tests_in_container", fake_container)
    monkeypatch.setattr("app.executor.docker_runner.docker_available", lambda: True)
    env = BugEnv(
        python="/opt/e/bin/python", image="img:latest", workdir="/testbed", network="bridge"
    )
    try:
        run_pytest(
            sys.executable, tmp_path, ["t.py::test_a"], report_path=tmp_path / "j.xml", env=env
        )
        assert captured["network"] == "bridge"
        # 不声明 network 的题:参数根本不出现,由 runner 的缺省 none 兜住
        run_pytest(
            sys.executable,
            tmp_path,
            ["t.py::test_a"],
            report_path=tmp_path / "j2.xml",
            env=BugEnv(python="/opt/e/bin/python", image="img:latest", workdir="/testbed"),
        )
        assert "network" not in captured
    finally:
        get_settings.cache_clear()


def test_docker_runner_uses_declared_network(monkeypatch, tmp_path: Path) -> None:
    """容器命令里的 --network 跟随参数,缺省仍是 none。"""
    from app.executor import docker_runner

    captured: dict = {}

    def fake_run_tests(command, cwd, timeout_seconds=None):
        captured["command"] = command
        return TestRunResult(
            command=command, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(docker_runner, "run_tests", fake_run_tests)
    docker_runner.run_tests_in_container(
        ws, ["t.py::t"], report_dir=tmp_path / "r", image="img:latest"
    )
    assert captured["command"][captured["command"].index("--network") + 1] == "none"

    captured.clear()
    docker_runner.run_tests_in_container(
        ws, ["t.py::t"], report_dir=tmp_path / "r2", image="img:latest", network="bridge"
    )
    assert captured["command"][captured["command"].index("--network") + 1] == "bridge"


def _record_chowns(monkeypatch, docker_runner) -> list[Path]:
    chowned: list[Path] = []
    monkeypatch.setattr(docker_runner, "_chown_to_executor", chowned.append)
    return chowned


def test_container_identity_aligns_uid_on_posix_nonroot(monkeypatch, tmp_path: Path) -> None:
    """原生 Linux 宿主(uid≠1000 且非 root):容器身份对齐目录属主,才写得进 junit。"""
    from app.executor import docker_runner

    chowned = _record_chowns(monkeypatch, docker_runner)
    monkeypatch.setattr(docker_runner, "_host_identity", lambda: (True, 1001, 1001))
    assert docker_runner._container_identity_cmd_flags(tmp_path, tmp_path / "r") == [
        "--user",
        "1001:1001",
        "-e",
        "HOME=/tmp",
    ]
    assert chowned == [], "非 root 宿主改不动别人属主,这条分支只能走 --user"


def test_container_identity_keeps_image_user_and_hands_dirs_over_when_root(
    monkeypatch, tmp_path: Path
) -> None:
    """宿主是 root(compose 里 API 容器没有 USER):不把被测代码提成 root,
    而是把本次 workspace 与 reports 的属主让给镜像的 pp(1000)。"""
    from app.executor import docker_runner

    chowned = _record_chowns(monkeypatch, docker_runner)
    monkeypatch.setattr(docker_runner, "_host_identity", lambda: (True, 0, 0))
    reports = tmp_path / "r"
    assert docker_runner._container_identity_cmd_flags(tmp_path, reports) == []
    assert chowned == [tmp_path, reports]


def test_container_identity_adds_nothing_on_non_posix(monkeypatch, tmp_path: Path) -> None:
    """Docker Desktop 的 bind mount 不按宿主 uid 校验:传 --user 只会撞上镜像里
    不存在的 gid,所以非 posix 一律不动容器身份。"""
    from app.executor import docker_runner

    chowned = _record_chowns(monkeypatch, docker_runner)
    monkeypatch.setattr(docker_runner, "_host_identity", lambda: (False, -1, -1))
    assert docker_runner._container_identity_cmd_flags(tmp_path, tmp_path / "r") == []
    assert chowned == []


def test_docker_run_command_carries_aligned_uid(monkeypatch, tmp_path: Path) -> None:
    """身份参数必须真的进到 docker run 的参数表,而不是只在 helper 里算对。"""
    from app.executor import docker_runner

    captured: dict = {}

    def fake_run_tests(command, cwd, timeout_seconds=None):
        captured["command"] = command
        return TestRunResult(
            command=command, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
        )

    monkeypatch.setattr(docker_runner, "_host_identity", lambda: (True, 1001, 1001))
    monkeypatch.setattr(docker_runner, "_chown_to_executor", lambda path: None)
    monkeypatch.setattr(docker_runner, "run_tests", fake_run_tests)
    ws = tmp_path / "ws"
    ws.mkdir()
    docker_runner.run_tests_in_container(ws, ["t.py::t"], report_dir=tmp_path / "r", image="i:1")
    cmd = captured["command"]
    assert cmd[cmd.index("--user") + 1] == "1001:1001"
    assert cmd.index("--user") < cmd.index("-v"), "--user 必须在镜像名之前、参数表之内"


def test_chown_to_executor_walks_the_tree_and_survives_unchownable(
    monkeypatch, tmp_path: Path
) -> None:
    """root 分支的 chown 要覆盖子项(被测仓库会往里写文件),单项失败不能掀掉整次执行。"""
    import os as _os

    from app.executor import docker_runner

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    seen: list[Path] = []

    def fake_chown(path, uid, gid, *, follow_symlinks=True):
        if Path(path).name == "a.py":
            raise PermissionError(1, "Operation not permitted")
        seen.append(Path(path))

    # raising=False:Windows 的 os 根本没有 chown 这个属性,而这条分支只在 posix 才走到
    monkeypatch.setattr(_os, "chown", fake_chown, raising=False)
    docker_runner._chown_to_executor(tmp_path)
    assert tmp_path in seen and tmp_path / "src" in seen


def test_run_pytest_rejects_container_env_on_local_backend(monkeypatch, tmp_path: Path) -> None:
    """声明了容器环境的题不允许退回宿主直跑:宿主没有那套年代精确依赖,
    跑出来的"失败"分不清是缺陷还是环境坏,正是 validate_entry 要拦的那类假信号。
    """
    from app.adapters.pytest_adapter import BugEnv, run_pytest
    from app.errors import ExecError

    env = BugEnv(python="/opt/x/bin/python", image="img:latest", workdir="/testbed")
    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "local")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ExecError) as exc_info:
            run_pytest(sys.executable, tmp_path, ["t.py::test_a"], env=env)
    finally:
        get_settings.cache_clear()
    assert "container" in str(exc_info.value)


def test_docker_runner_mounts_workspace_at_declared_workdir(monkeypatch, tmp_path: Path) -> None:
    """workdir 决定工作区在容器里的挂载点与 cwd;缺省仍是 /ws(存量行为不变)。"""
    from app.executor import docker_runner

    captured: dict = {}

    def fake_run_tests(command, cwd, timeout_seconds=None):
        captured["command"] = command
        return TestRunResult(
            command=command, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    reports = tmp_path / "reports"
    monkeypatch.setattr(docker_runner, "run_tests", fake_run_tests)
    docker_runner.run_tests_in_container(
        ws,
        ["t.py::test_a"],
        report_dir=reports,
        image="img:latest",
        python_bin="/opt/envs/testbed/bin/python",
        workdir="/testbed",
    )
    cmd = captured["command"]
    assert f"{ws.resolve()}:/testbed" in cmd, "工作区必须挂到题目声明的预装路径上"
    assert "/ws" not in cmd
    assert cmd[cmd.index("-w") + 1] == "/testbed"
    assert cmd[cmd.index("img:latest") + 1] == "/opt/envs/testbed/bin/python"

    # 缺省形态:不传 workdir 时保持既有 /ws 挂载
    captured.clear()
    docker_runner.run_tests_in_container(
        ws, ["t.py::test_a"], report_dir=reports, image="img:latest"
    )
    assert f"{ws.resolve()}:/ws" in captured["command"]


def test_run_pytest_host_env_python_overrides_interpreter(monkeypatch, tmp_path: Path) -> None:
    """宿主题(env 只给 python)用题目自带解释器跑,而不是 sys.executable。"""
    from app.adapters.pytest_adapter import BugEnv, run_pytest

    captured: dict = {}

    def fake_run_tests(command, cwd, timeout_seconds=None):
        captured["command"] = command
        return TestRunResult(
            command=command, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1
        )

    env = BugEnv(python="D:/envs/demo/Scripts/python.exe")
    monkeypatch.setenv("PATCHPILOT_EXECUTION_BACKEND", "local")
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("app.adapters.pytest_adapter.run_tests", fake_run_tests)
    try:
        run_pytest(
            sys.executable, tmp_path, ["t.py::test_a"], report_path=tmp_path / "j.xml", env=env
        )
    finally:
        get_settings.cache_clear()

    assert captured["command"][0] == "D:/envs/demo/Scripts/python.exe"
