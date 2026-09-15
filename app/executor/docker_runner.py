"""Docker 容器化测试执行器:每次验证一个临时容器,用后即删。

隔离边界(企划书第 9 节):
- --network none   :容器内不能联网、不能访问宿主网络;
- --memory/--cpus  :资源限额,防止失控测试拖垮宿主;
- 只挂载任务工作区 :容器内看不到宿主其他文件;
- --rm             :容器退出即销毁,不残留状态。

junit 报告通过挂载的 reports 目录传回宿主(不落在被验证的工作区内)。
"""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from app.adapters.pytest_adapter import PytestReport, parse_junit_xml
from app.config import get_settings
from app.executor.local_runner import run_tests

log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    """docker CLI 存在且守护进程可达(结果缓存)。"""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_tests_in_container(
    workspace: Path | str,
    test_ids: list[str],
    *,
    image: str | None = None,
    report_dir: Path | str,
    timeout_seconds: int | None = None,
    memory: str = "1g",
    cpus: str = "1.0",
    python_bin: str = "python",
) -> tuple[PytestReport, object]:
    """在临时容器中执行指定测试集;返回 (解析后的报告, 原始运行结果)。

    与 local_runner 的差异只在隔离边界:命令组装、报告解析完全复用。
    """
    ws = Path(workspace).resolve()
    reports = Path(report_dir).resolve()
    reports.mkdir(parents=True, exist_ok=True)
    junit = reports / f"junit-{uuid.uuid4().hex}.xml"
    image = image or get_settings().docker_image
    timeout = timeout_seconds or get_settings().test_timeout_seconds

    cmd = [
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
        f"{ws}:/ws",
        "-v",
        f"{reports}:/reports",
        "-w",
        "/ws",
        image,
        python_bin,
        "-m",
        "pytest",
        "-q",
        "--color=no",
        f"--junitxml=/reports/{junit.name}",
        *test_ids,
    ]
    log.info("docker run_tests: %s test(s) in image=%s", len(test_ids), image)
    run = run_tests(cmd, cwd=ws, timeout_seconds=timeout)
    report = parse_junit_xml(junit)
    report.exit_code = run.exit_code
    report.duration_ms = run.duration_ms
    report.timed_out = run.timed_out
    return report, run
