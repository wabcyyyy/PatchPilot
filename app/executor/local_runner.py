"""测试命令执行器:subprocess 直跑,超时杀进程树,输出截断。

安全边界(与 app/executor/whitelist.py 配合):
- 只接受参数列表,永不 `shell=True`——字符串拼接是命令注入的根源;
- 超时必须清理整棵进程树,否则 pytest 拉起的子进程会在 Windows 上变成孤儿。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.errors import ExecError

log = logging.getLogger(__name__)

# N-8 整改:被诊断仓库的内容是不可信输入,子进程默认继承 API 进程全量环境——
# PATCHPILOT_API_TOKEN / PATCHPILOT_LLM_API_KEY 会被 conftest/测试代码
# `os.environ` 读走并写进工作区文件外带。改为最小白名单:只保留运行
# pytest/工具链所需的系统级变量(不含任何平台密钥)。
# home 系变量(R2 整改):Windows 服务/cmd 启动的进程通常没有 HOME,
# 缺 USERPROFILE 会让子进程 Path.home()/expanduser('~') 失败。
# DOCKER_* 系(R2 整改):docker 后端的 docker run 也走本执行器,
# 远端 daemon(DOCKER_HOST/TLS)部署下没有它们会出现"可用性检查通过、执行必败"。
_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "NUMBER_OF_PROCESSORS",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
)


@dataclass
class TestRunResult:
    """一次测试执行的原始结果(尚未解析报告)。"""

    command: list[str]
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int
    timed_out: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def dropped_bytes(self) -> int:
        return self.stdout_dropped_bytes + self.stderr_dropped_bytes


def _truncate(text: str) -> str:
    limit = get_settings().max_output_chars
    if len(text) <= limit:
        return text
    return text[-limit:]


class BoundedStreamDrainer:
    """有界流式管道读取器: 独立线程分块读取，内存保留有界 tail，统计丢弃字节数。"""

    def __init__(self, stream: Any, max_tail_bytes: int) -> None:
        self.stream = stream
        self.max_tail_bytes = max_tail_bytes
        self.total_bytes = 0
        self.dropped_bytes = 0
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._done = threading.Event()

    def drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    break
                with self._lock:
                    self.total_bytes += len(chunk)
                    self._tail.extend(chunk)
                    if len(self._tail) > self.max_tail_bytes:
                        excess = len(self._tail) - self.max_tail_bytes
                        del self._tail[:excess]
                        self.dropped_bytes += excess
        except (ValueError, OSError):
            pass
        finally:
            self._done.set()

    def get_tail_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._tail)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """杀掉整棵进程树:Windows 用 taskkill /T,POSIX 用进程组信号。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        import os
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.error("process %s survived kill; continuing", proc.pid)


def run_tests(
    command: list[str],
    cwd: Path | str,
    timeout_seconds: int | None = None,
) -> TestRunResult:
    """在 cwd 下执行测试命令(列表形式),返回原始结果。

    超时是正常业务结果(timed_out=True),不是异常;
    只有无法启动进程这类基础设施故障才抛 ExecError。
    """
    if not command:
        raise ExecError("empty command")
    # 注入防御不在这里:runner 是通用执行器,合法的 `python -c` 代码可含换行;
    # 面向 Agent 的命令边界由 whitelist.check_cmd_allowed 在工具层强制。

    timeout = timeout_seconds or get_settings().test_timeout_seconds
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    started = time.monotonic()
    log.info("run_tests: %s (cwd=%s, timeout=%ss)", command, cwd, timeout)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        raise ExecError(f"cannot start {command[0]!r}: {exc}") from exc

    limit_chars = get_settings().max_output_chars
    max_tail_bytes = max(limit_chars * 4, 65536)

    stdout_drainer = BoundedStreamDrainer(proc.stdout, max_tail_bytes)
    stderr_drainer = BoundedStreamDrainer(proc.stderr, max_tail_bytes)

    t_out = threading.Thread(target=stdout_drainer.drain, daemon=True)
    t_err = threading.Thread(target=stderr_drainer.drain, daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    deadline = started + timeout
    pipe_hung = False

    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                timed_out = True
                log.warning("run_tests timed out after %ss: %s", timeout, command)
                _kill_tree(proc)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    # N-15/R2 整改: 逃逸出进程组的进程持有管道写端时，kill 杀不到；
                    # proc.kill() 尝试强杀直接子进程，超时改走关管道回收兜底
                    proc.kill()
                    pipe_hung = True
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=2.0)
                    if proc.stdout and not getattr(proc.stdout, "closed", False):
                        proc.stdout.close()
                    if proc.stderr and not getattr(proc.stderr, "closed", False):
                        proc.stderr.close()
                break

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    if proc.stdout and not getattr(proc.stdout, "closed", False):
        proc.stdout.close()
    if proc.stderr and not getattr(proc.stderr, "closed", False):
        proc.stderr.close()

    duration_ms = int((time.monotonic() - started) * 1000)
    if pipe_hung:
        stdout_tail_str = ""
        stderr_tail_str = ""
    else:
        stdout_tail_str = _truncate(
            stdout_drainer.get_tail_bytes().decode("utf-8", errors="replace")
        )
        stderr_tail_str = _truncate(
            stderr_drainer.get_tail_bytes().decode("utf-8", errors="replace")
        )

    result = TestRunResult(
        command=command,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout_tail=stdout_tail_str,
        stderr_tail=stderr_tail_str,
        duration_ms=duration_ms,
        timed_out=timed_out,
        stdout_bytes=stdout_drainer.total_bytes,
        stderr_bytes=stderr_drainer.total_bytes,
        stdout_dropped_bytes=stdout_drainer.dropped_bytes,
        stderr_dropped_bytes=stderr_drainer.dropped_bytes,
    )
    log.info(
        "run_tests done rc=%s in %sms (timed_out=%s)", result.exit_code, duration_ms, timed_out
    )
    return result
