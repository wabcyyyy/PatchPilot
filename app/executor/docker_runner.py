"""Docker 容器化测试执行器:每次验证一个临时容器,用后即删。

隔离边界(企划书第 9 节):
- --network none   :容器内不能联网、不能访问宿主网络(缺省值,也是唯一允许的运行形态);
                   仅当题目 manifest 显式声明 env.network 时才可放行,而 manifest 是
                   运维本地资产,模型与 API 请求都写不到它(外部数据集里确有必须出网的
                   对照组测试,如 sphinx 的 linkcheck)
- --memory/--cpus  :资源限额,防止失控测试拖垮宿主;
- 只挂载任务工作区 :容器内看不到宿主其他文件;
- --rm             :容器退出即销毁,不残留状态。

junit 报告通过挂载的 reports 目录传回宿主(不落在被验证的工作区内)。
容器身份与宿主目录属主必须对齐,否则宿主是原生 Linux 时容器写不进 reports——
见 `_container_identity_cmd_flags` 与 docs/docker-backend-notes.md。
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from app.adapters.pytest_adapter import PytestReport, parse_junit_xml
from app.config import get_settings
from app.executor.local_runner import TestRunResult, run_tests

log = logging.getLogger(__name__)

# 合成 case 的 traceback 上限,与 pytest_adapter 里 junit <failure> 正文的截断口径一致
_STDERR_TAIL_CHARS = 4000

# docker/executor.Dockerfile 里 `useradd --create-home --uid 1000 pp` 的固定 uid:
# 镜像不给被测代码 root 权限,代价是它必须"看得见且写得进"宿主给它的两个目录。
EXECUTOR_UID = 1000


def _host_identity() -> tuple[bool, int, int]:
    """(是否 posix, 本进程 uid, 本进程 gid)。Windows 上后两项无意义,返回 -1。"""
    if os.name != "posix":
        return False, -1, -1
    return True, os.getuid(), os.getgid()


def _chown_to_executor(path: Path) -> None:
    """把本次运行的目录(含子项)让给镜像里的 pp(1000);只在宿主是 root 时用到。

    root 有 CAP_CHOWN,能改属主;而非 root 宿主改不动别人的属主,那条路走 `--user`。
    """
    try:
        os.chown(path, EXECUTOR_UID, EXECUTOR_UID, follow_symlinks=False)
    except OSError as exc:
        log.warning("docker: chown %s 失败,容器可能写不进该目录:%s", path, exc)
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            # 指向外部的符号链接等改不动的项:保持原状,不掀掉整次执行
            with contextlib.suppress(OSError):
                os.chown(Path(dirpath) / name, EXECUTOR_UID, EXECUTOR_UID, follow_symlinks=False)


def _container_identity_cmd_flags(workspace: Path, reports: Path) -> list[str]:
    """让容器进程与工作区/报告目录的属主是同一个身份,且两条分支都不给 root。

    背景:镜像固定 uid 1000,而这两个目录由本进程创建。原生 Linux 上本进程 uid
    ≠ 1000 时,junit 写不进 → pytest 跑完却以 exit 1 收场、报告文件不存在(表现是
    "docker_available 通过、每次执行必败");Docker Desktop 的文件共享层不按宿主 uid
    校验,所以本地怎么跑都是绿的。
    - 本进程非 root:用 `--user` 把容器 uid 对齐到本进程,顺带让工作区也可写
      (被测仓库里要落盘的测试才跑得动)。HOME 指到 /tmp:对齐来的 uid 并不拥有
      镜像里的 /home/pp,写缓存到 $HOME 的测试会因此失败。
    - 本进程是 root(compose 部署里 API 容器没有 USER):保持 pp(1000)不动,
      把本次 workspace 与 reports 的属主让给 1000。
    """
    is_posix, uid, gid = _host_identity()
    if not is_posix:
        return []
    if uid != 0:
        return ["--user", f"{uid}:{gid}", "-e", "HOME=/tmp"]
    _chown_to_executor(workspace)
    _chown_to_executor(reports)
    return []


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


def _attach_container_stderr(report: PytestReport, run: TestRunResult) -> None:
    """容器没交出 junit 报告时,把它的退出码与 stderr 尾巴挂到那条合成 case 上。

    判定口径一个字不动(`signature`/`all_passed` 都不读 traceback),但真实死因至少
    看得见:CI run #5..#18 十四次连红的根因(`PermissionError: '/reports/x.xml'`)
    原本只存在于被我丢掉的 stderr 里,报告里只剩一句"no junit xml"——那是症状不是原因。
    实测分流:这条 traceback 走 **stderr**,而"哪个测试失败了"走 stdout,所以只取 stderr。
    """
    if not report.failed_cases:
        return
    tail = (run.stderr_tail or "").strip()
    lines = [f"容器未生成 junit 报告;docker run 退出码 {run.exit_code}"]
    if tail:
        lines.append(tail)
    report.failed_cases[0].traceback = "\n".join(lines)[-_STDERR_TAIL_CHARS:]


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
    workdir: str = "/ws",
    network: str = "none",
) -> tuple[PytestReport, object]:
    """在临时容器中执行指定测试集;返回 (解析后的报告, 原始运行结果)。

    与 local_runner 的差异只在隔离边界:命令组装、报告解析完全复用。
    network 缺省 none(平台隔离边界),仅题目 manifest 显式声明才可放行,且受枚举约束。
    workdir 是工作区在容器内的挂载点:外部数据集镜像(如 SWE-bench)把仓库预装在固定
    路径并按该路径做了 editable 安装,必须把打过补丁的工作区挂到同一路径,否则
    import 到的仍是镜像里未修改的那份代码。
    """
    ws = Path(workspace).resolve()
    reports = Path(report_dir).resolve()
    reports.mkdir(parents=True, exist_ok=True)
    # R2 整改:0o777 → 0o755——报告目录对 other 只读,junit 内容不可被无关用户改写。
    # 容器能写它靠的不是放开这里的权限,而是下面把容器身份对齐到目录属主
    # (`_container_identity_cmd_flags`);Docker Desktop 的文件共享层不校验宿主 uid,
    # 所以只有原生 Linux 宿主能看出这条契约有没有满足。
    reports.chmod(0o755)
    junit = reports / f"junit-{uuid.uuid4().hex}.xml"
    image = image or get_settings().docker_image
    timeout = timeout_seconds or get_settings().test_timeout_seconds

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "--memory",
        memory,
        "--cpus",
        cpus,
        *_container_identity_cmd_flags(ws, reports),
        "-v",
        f"{ws}:{workdir}",
        "-v",
        f"{reports}:/reports",
        "-w",
        workdir,
        image,
        python_bin,
        "-m",
        "pytest",
        "-q",
        "--color=no",
        "-o",
        "junit_family=xunit1",
        f"--junitxml=/reports/{junit.name}",
        *test_ids,
    ]
    log.info("docker run_tests: %s test(s) in image=%s", len(test_ids), image)
    run = run_tests(cmd, cwd=ws, timeout_seconds=timeout)
    report = parse_junit_xml(junit)
    if not junit.exists():
        _attach_container_stderr(report, run)
    report.requested_ids = list(test_ids)
    report.exit_code = run.exit_code
    report.duration_ms = run.duration_ms
    report.timed_out = run.timed_out
    return report, run
