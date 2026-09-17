"""执行后端分发骨架:按 Settings.execution_backend 把测试命令路由到 local/docker。

本卡只交付分发器与 docker 命令拼装(离线单测覆盖);把执行调用点真正切到
docker 后端属于禁区周边的集成改造,留待白天人工验证,接线点分析见
docs/docker-backend-notes.md。隔离旗标与 app/executor/docker_runner.py 保持一致
(--rm --network none --memory --cpus,只挂载工作区)。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import get_settings
from app.errors import ExecError
from app.executor.local_runner import TestRunResult, run_tests

log = logging.getLogger(__name__)


def build_docker_command(
    command: list[str],
    *,
    image: str,
    workspace: Path | str,
    memory: str = "1g",
    cpus: str = "1.0",
) -> list[str]:
    """把宿主侧测试命令包进 `docker run` 参数(工作区挂载到容器内同路径)。"""
    ws = str(Path(workspace).resolve())
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        memory,
        "--cpus",
        cpus,
        "-v",
        f"{ws}:{ws}",
        "-w",
        ws,
        image,
        *command,
    ]


def run_tests_by_backend(
    command: list[str],
    cwd: Path | str,
    timeout_seconds: int | None = None,
) -> TestRunResult:
    """按配置分发测试执行:local 直跑;docker 经 `docker run` 包一层后执行。

    `docker run` 本身是宿主子进程,因此 docker 后端同样经由 local_runner.run_tests
    执行拼装后的命令——与 docker_runner.run_tests_in_container 的内部实现同构。
    """
    if not command:
        raise ExecError("empty command")
    settings = get_settings()
    if settings.execution_backend == "local":
        return run_tests(command, cwd, timeout_seconds)
    if settings.execution_backend == "docker":
        from app.executor.docker_runner import docker_available

        if not docker_available():
            raise ExecError("execution_backend='docker' but docker daemon is not available")
        docker_command = build_docker_command(
            command,
            image=settings.docker_image,
            workspace=Path(cwd).resolve(),
        )
        log.info("backend=docker: %s", docker_command[:2])
        return run_tests(docker_command, cwd, timeout_seconds)
    raise ExecError(f"unknown execution backend: {settings.execution_backend!r}")
