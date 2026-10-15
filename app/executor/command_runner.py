"""受控命令执行器(G09):argv 级执行、进程/容器生命周期与原始输出落盘。

与 `local_runner.run_tests` 的分工:run_tests 是平台既有测试链(基线/verify)的执行器,
保留旧白名单口径;本模块是 G09 新增 run_command 工具的执行器,面向诊断命令:

- **原始输出先落盘**:stdout/stderr 以流式方式写进 scratch 的 .raw 文件,受磁盘额度
  (per-stream 上限)约束;超出的字节如实计数,不做静默丢弃。模型可见的 tail 在这里
  之后才裁——超过 tail 上限的输出可用 evidence_id 分页取回(G05 协议,G09 接线);
- **生命周期**:timeout 与 cancel 事件都终止**本任务自己的**进程树/容器——Windows 走
  taskkill /T,POSIX 走进程组;Docker 按 --name 精确 `docker kill`,禁止全局 kill 或
  docker prune;
- **容器边界**:网络关闭、无宿主密钥/env 透传、无 docker socket、非 root、CPU/内存/
  PID/时间/输出额度;可写挂载只有 cwd 指向的任务副本或 scratch。Docker 不可用 ⇒
  能力不可用(environment_not_ready),不自动退回宿主执行。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.errors import ExecError
from app.executor.local_runner import _ENV_ALLOWLIST, _kill_tree

log = logging.getLogger(__name__)

# 单次 wait 轮询切片:cancel/timeout 的收敛粒度,也是避免 busy-loop 的下限
_POLL_SECONDS = 0.2
# G12 取消收敛报告门槛(毫秒):"cancel 请求后 5 秒内终止"是产品的验收目标;
# 超过就在结果里如实报 cancellation_pending,不虚报"已及时终止"。
CANCELLATION_PENDING_THRESHOLD_MS = 5000
# docker run 的资源上限(企划书第 9 节同族;PID 上限防 fork 炸弹)
_CONTAINER_MEMORY = "1g"
_CONTAINER_CPUS = "1.0"
_CONTAINER_PIDS = "128"


@dataclass
class CommandRunResult:
    """一次受控命令的原始结果:事实读数,不含任何验收语义。"""

    argv: list[str]
    exit_code: int | None
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    pid: int = 0
    container_name: str = ""
    backend: str = "local"
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    env_keys: list[str] = field(default_factory=list)
    # G12:从"检测到取消/超时"到进程树/容器**确认退出**的耗时;
    # 超过阈值时 cancellation_pending=True(终止未在目标窗口内完成,如实上报)
    kill_latency_ms: int = 0
    cancellation_pending: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": self.argv,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "pid": self.pid,
            "container_name": self.container_name,
            "backend": self.backend,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
            "env_keys": self.env_keys,
            "kill_latency_ms": self.kill_latency_ms,
            "cancellation_pending": self.cancellation_pending,
        }


def _drain_to_cap(stream: object, path: Path, cap_bytes: int, out: dict[str, int]) -> None:
    """把一个流写进磁盘,超过额度后只计数不落盘;结果写回 out(落盘/丢弃字节)。"""
    written = 0
    dropped = 0
    with open(path, "wb") as fh:
        while True:
            chunk = stream.read(65536)  # type: ignore[attr-defined]
            if not chunk:
                break
            if written < cap_bytes:
                keep = chunk[: cap_bytes - written]
                fh.write(keep)
                written += len(keep)
                dropped += len(chunk) - len(keep)
            else:
                dropped += len(chunk)
    out["written"] = written
    out["dropped"] = dropped


def _wait_bounded(
    proc: subprocess.Popen[bytes], timeout: float, cancel_event: threading.Event | None
) -> str:
    """等待进程退出;返回 "exited" | "timeout" | "cancelled"。"""
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        try:
            proc.wait(timeout=_POLL_SECONDS)
            return "exited"
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            return "timeout"


def _docker_run_argv(
    *,
    argv: list[str],
    image: str,
    cwd: Path,
    container_name: str,
    workdir: str = "/ws",
    env_extra: dict[str, str] | None = None,
) -> list[str]:
    """组装加固的 docker run:网络关、无 env 透传、无 socket、非 root、有资源上限。

    cwd 挂在 /ws 并作为容器工作目录:local 与 docker 两条路的命令形态因此同构。
    env_extra 是显式附加的容器变量(如 PYTHONPATH=/ws),逐条 -e 传入,不碰宿主环境。
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.deployment_mode == "production":
        trusted = settings.get_trusted_docker_images()
        if not image or (trusted and image not in trusted):
            raise ExecError(
                f"docker image '{image}' is not in trusted_docker_images in production mode"
            )

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",
        "--memory",
        getattr(settings, "docker_memory", _CONTAINER_MEMORY) or _CONTAINER_MEMORY,
        "--cpus",
        getattr(settings, "docker_cpus", _CONTAINER_CPUS) or _CONTAINER_CPUS,
        f"--pids-limit={getattr(settings, 'docker_pids_limit', _CONTAINER_PIDS) or _CONTAINER_PIDS}",
        "--security-opt",
        getattr(settings, "docker_security_opt", "no-new-privileges:true"),
        "--cap-drop",
        getattr(settings, "docker_cap_drop", "ALL"),
        "-v",
        f"{cwd}:{workdir}",
        "-w",
        workdir,
    ]
    for key, value in sorted((env_extra or {}).items()):
        cmd += ["-e", f"{key}={value}"]
    if os.name == "posix" and os.getuid() != 0:
        # 宿主非 root:容器身份对齐本进程(与 docker_runner 的判定同源),HOME 指向 /tmp
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    else:
        cmd += ["--user", "1000:1000", "-e", "HOME=/tmp"]
    return [*cmd, image, *argv]


def _docker_kill(container_name: str) -> None:
    """只杀本任务自己的容器(按名字精确匹配);失败记日志,不掩盖主流程的失败形态。"""
    try:
        subprocess.run(
            ["docker", "kill", "--", container_name],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("docker kill %s failed: %s", container_name, exc)


def run_controlled_command(
    argv: list[str],
    *,
    cwd: Path,
    scratch_dir: Path,
    timeout_seconds: int,
    output_cap_bytes: int,
    cancel_event: threading.Event | None = None,
    backend: str = "local",
    image: str = "",
    seq: int = 0,
    env_extra: dict[str, str] | None = None,
) -> CommandRunResult:
    """执行一条受控命令;超时/取消终止本任务的进程树或容器,原始输出先落盘。

    backend="docker" 时 cwd(任务诊断副本或 scratch)挂到容器 /ws;镜像缺依赖由命令
    自身的失败形态暴露,调用方按 environment_not_ready 结构化,这里不做探测。
    env_extra 是任务**显式构造**的附加变量(如 PYTHONPATH 指向诊断副本),不是宿主
    环境透传——宿主密钥永远不在白名单里,local 分支的 env 仍从 _ENV_ALLOWLIST 起步。
    只有无法启动进程这类基础设施故障才抛 ExecError。
    """
    if not argv:
        raise ExecError("empty command")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = scratch_dir / f"cmd-{seq}-stdout.raw"
    stderr_path = scratch_dir / f"cmd-{seq}-stderr.raw"
    container_name = f"pp-cmd-{uuid.uuid4().hex[:12]}"

    if backend == "docker":
        popen_argv = _docker_run_argv(
            argv=argv,
            image=image,
            cwd=Path(cwd),
            container_name=container_name,
            env_extra=env_extra,
        )
        run_cwd: Path | None = None  # docker run 不需要宿主 cwd
        env: dict[str, str] | None = None  # 零透传:连 PATH 都不继承(镜像自带)
    else:
        popen_argv = list(argv)
        run_cwd = Path(cwd)
        env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
        if env_extra:
            env.update(env_extra)

    started = time.monotonic()
    log.info("run_command[%s]: %s (cwd=%s, timeout=%ss)", backend, argv, cwd, timeout_seconds)
    kwargs: dict[str, object] = {}
    if backend == "local":
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            popen_argv,
            cwd=str(run_cwd) if run_cwd is not None else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        raise ExecError(f"cannot start {argv[0]!r}: {exc}") from exc

    stdout_meta: dict[str, int] = {}
    stderr_meta: dict[str, int] = {}
    readers = [
        threading.Thread(
            target=_drain_to_cap, args=(proc.stdout, stdout_path, output_cap_bytes, stdout_meta)
        ),
        threading.Thread(
            target=_drain_to_cap, args=(proc.stderr, stderr_path, output_cap_bytes, stderr_meta)
        ),
    ]
    for reader in readers:
        reader.start()
    outcome = _wait_bounded(proc, float(timeout_seconds), cancel_event)
    timed_out = outcome == "timeout"
    # G12:从"检测到取消/超时"到"确认退出"的耗时是产品质量读数,不是验收语义
    kill_started = time.monotonic()
    if outcome in ("timeout", "cancelled"):
        if backend == "docker":
            _docker_kill(container_name)
        else:
            _kill_tree(proc)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # 逃逸出进程组的分离孙子进程持着管道写端(N-15 同族):关流让 reader 收尾
            log.error("command %s survived kill; closing pipes to unblock readers", argv[0])
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
    kill_latency_ms = (
        int((time.monotonic() - kill_started) * 1000) if outcome in ("timeout", "cancelled") else 0
    )
    for reader in readers:
        reader.join(timeout=10)
    duration_ms = int((time.monotonic() - started) * 1000)
    result = CommandRunResult(
        argv=argv,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=outcome == "cancelled",
        pid=proc.pid,
        container_name=container_name if backend == "docker" else "",
        backend=backend,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        stdout_bytes=stdout_meta.get("written", 0),
        stderr_bytes=stderr_meta.get("written", 0),
        stdout_dropped_bytes=stdout_meta.get("dropped", 0),
        stderr_dropped_bytes=stderr_meta.get("dropped", 0),
        env_keys=sorted(env) if env is not None else [],
        kill_latency_ms=kill_latency_ms,
        # 超过目标窗口仍在收敛 = 如实报 pending,不虚报"已 5 秒内终止"
        cancellation_pending=kill_latency_ms > CANCELLATION_PENDING_THRESHOLD_MS,
    )
    log.info(
        "run_command done rc=%s in %sms (timed_out=%s cancelled=%s kill_ms=%s pending=%s backend=%s)",
        result.exit_code,
        duration_ms,
        timed_out,
        result.cancelled,
        kill_latency_ms,
        result.cancellation_pending,
        backend,
    )
    return result
