"""P08: 执行沙箱与镜像策略测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.errors import ExecError
from app.executor.command_runner import _docker_run_argv
from app.executor.docker_runner import (
    build_docker_test_cmd,
    docker_available,
)


def test_build_docker_test_cmd_hardened_flags(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATCHPILOT_DEPLOYMENT_MODE", "development")
    monkeypatch.setenv("PATCHPILOT_DOCKER_PIDS_LIMIT", "128")
    monkeypatch.setenv("PATCHPILOT_DOCKER_SECURITY_OPT", "no-new-privileges:true")
    monkeypatch.setenv("PATCHPILOT_DOCKER_CAP_DROP", "ALL")

    from app.config import get_settings

    get_settings.cache_clear()

    ws = tmp_path / "ws"
    reports = tmp_path / "reports"
    ws.mkdir()
    reports.mkdir()

    cmd = build_docker_test_cmd(
        workspace=ws,
        reports=reports,
        junit_name="junit.xml",
        test_ids=["tests/test_foo.py::test_bar"],
        image="python:3.11-slim",
        container_name="pp-test-container-01",
    )

    cmd_str = " ".join(cmd)
    # 1. 默认断网
    assert "--network none" in cmd_str
    # 2. PID 与资源保护
    assert "--pids-limit=128" in cmd_str
    assert "--memory 1g" in cmd_str
    assert "--cpus 1.0" in cmd_str
    # 3. 禁提权与丢弃 capabilities
    assert "--security-opt no-new-privileges:true" in cmd_str
    assert "--cap-drop ALL" in cmd_str
    # 4. 非 root 身份执行 (强制非 root)
    assert "--user" in cmd
    user_idx = cmd.index("--user")
    user_val = cmd[user_idx + 1]
    assert user_val != "0:0" and not user_val.startswith("0:")
    # 5. 命名追踪
    assert "--name pp-test-container-01" in cmd_str
    # 6. 可写挂载受限 (只挂载 ws 和 reports)
    assert f"-v {ws}:/ws" in cmd_str
    assert f"-v {reports}:/reports" in cmd_str


def test_docker_production_mode_image_allowlist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATCHPILOT_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv(
        "PATCHPILOT_TRUSTED_DOCKER_IMAGES", "python:3.11-slim,patchpilot-executor:latest"
    )
    from app.config import get_settings

    get_settings.cache_clear()

    ws = tmp_path / "ws"
    reports = tmp_path / "reports"
    ws.mkdir()
    reports.mkdir()

    # 1. 白名单内镜像放行
    cmd = build_docker_test_cmd(
        workspace=ws,
        reports=reports,
        junit_name="junit.xml",
        test_ids=["test.py"],
        image="python:3.11-slim",
        container_name="pp-test-valid",
    )
    assert "python:3.11-slim" in cmd

    # 2. 未在白名单镜像直接拒绝抛出 ExecError
    with pytest.raises(ExecError, match="not in trusted_docker_images in production mode"):
        build_docker_test_cmd(
            workspace=ws,
            reports=reports,
            junit_name="junit.xml",
            test_ids=["test.py"],
            image="malicious/untrusted-image:latest",
            container_name="pp-test-invalid",
        )

    # 3. command_runner 的 _docker_run_argv 同样拦截未在白名单镜像
    with pytest.raises(ExecError, match="not in trusted_docker_images in production mode"):
        _docker_run_argv(
            argv=["ls"],
            image="malicious/untrusted-image:latest",
            cwd=ws,
            container_name="pp-cmd-invalid",
        )


@pytest.mark.skipif(not docker_available(), reason="Docker daemon 不可用")
def test_docker_sandbox_runtime_isolation() -> None:
    """在真实容器内验证沙箱安全加固: 非 root、断网、禁提权。"""
    image = "patchpilot-executor:latest"

    # 1. 验证真实容器以非 root (uid != 0) 运行
    proc = subprocess.run(
        ["docker", "run", "--rm", "--user", "1000:1000", image, "id", "-u"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1000"

    # 2. 验证 --network none 真实阻断外网
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('NET_OK')\n"
        "except OSError:\n"
        "    print('NET_BLOCKED')\n"
    )
    proc_net = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image, "python", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc_net.returncode == 0
    assert "NET_BLOCKED" in proc_net.stdout
