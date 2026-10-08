"""受理预检(S06):不执行模型、不安装依赖,只验证环境能不能把两个测试集跑起来。

分类边界(spec S06 硬要求):
- **依赖缺失 / 收集错误**(环境问题)与**业务断言失败**是两类事实——预检只看
  前者;测试集的真实 baseline(红/绿形态)必须仍由引擎执行,预检不能替代 baseline;
- 预检失败 → 受理即拒绝(422),不建任务行、不调模型、不留假 RUNNING;
- collect 走受控执行流程(executor 的 run_tests),不在 HTTP handler 里裸 subprocess;
- 预检可以较慢(要物化/收集),但不得持数据库锁或幂等锁——调用方在锁外调用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.pytest_adapter import RC_NO_TESTS_COLLECTED, build_pytest_cmd
from app.errors import ExecError, InvalidRequestError
from app.executor.local_runner import run_tests

log = logging.getLogger(__name__)

# pytest 退出码语义与 adapters 一致:5=未收集到,4=用法错误(id/路径非法),
# 1/2/3=收集/中断/内部错误——对预检而言都是"环境或用例定位问题",不是断言失败。
RC_USAGE_ERROR = 4


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PreflightReport:
    ok: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    duration_ms: int = 0

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]


def _collect_check(
    name: str,
    python_exe: str,
    workspace: Path,
    test_ids: list[str],
    timeout_seconds: int,
) -> PreflightCheck:
    """一个测试集的收集检查:--collect-only 由 executor 受控执行。

    rc=4(usage error)要按输出细分:收集期 import 失败(依赖缺失)也报 4,
    但 stdout 带 ERROR 块——那是环境问题;不带 ERROR 的 4 才是"id/路径不存在"。
    """
    cmd = build_pytest_cmd(
        python_exe,
        test_ids,
        None,
        ["--collect-only", "-q", "-p", "no:cacheprovider"],
    )
    try:
        run = run_tests(cmd, workspace, timeout_seconds)
    except ExecError as exc:
        return PreflightCheck(name=name, ok=False, detail=f"cannot execute pytest: {exc}")
    if run.exit_code == 0:
        return PreflightCheck(name=name, ok=True)
    if run.exit_code == RC_NO_TESTS_COLLECTED:
        return PreflightCheck(name=name, ok=False, detail="no tests collected for the set")
    stdout = run.stdout_tail or ""
    if run.exit_code == RC_USAGE_ERROR and "ERROR" in stdout:
        return PreflightCheck(
            name=name,
            ok=False,
            detail=(
                "collection error (missing dependency / import failure);"
                " this is an environment problem, not an assertion failure"
            ),
        )
    if run.exit_code == RC_USAGE_ERROR:
        return PreflightCheck(
            name=name, ok=False, detail="pytest usage error (bad test id or target path)"
        )
    return PreflightCheck(
        name=name,
        ok=False,
        detail=(
            "collection error (missing dependency / import failure / interrupt);"
            " this is an environment problem, not an assertion failure"
        ),
    )


def run_preflight(
    *,
    python_exe: str,
    workspace: Path,
    failed_tests: list[str],
    regression_tests: list[str],
    container_image: str | None = None,
    timeout_seconds: int = 60,
) -> PreflightReport:
    """对冻结输入做受理预检;任一项不过即抛 InvalidRequestError(带全部失败原因)。

    收集检查在**一次性临时副本**上执行:pytest import conftest 会写 `__pycache__`,
    直接在冻结副本上跑会污染其内容指纹(实测踩中)——副本用完即删,冻结正本零写入。
    """
    import shutil
    import tempfile

    started = time.monotonic()
    scratch = Path(tempfile.mkdtemp(prefix="preflight-collect-"))
    try:
        shutil.copytree(workspace, scratch / "ws", symlinks=True)
        checks = _run_checks(
            python_exe=python_exe,
            workspace=scratch / "ws",
            failed_tests=failed_tests,
            regression_tests=regression_tests,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    report = PreflightReport(
        ok=all(c.ok for c in checks),
        checks=checks,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if not report.ok:
        raise InvalidRequestError("preflight failed: " + "; ".join(report.failures()))
    log.info(
        "preflight ok in %d ms (%s)",
        report.duration_ms,
        ", ".join(f"{c.name}=ok" for c in report.checks),
    )
    return report


def _run_checks(
    *,
    python_exe: str,
    workspace: Path,
    failed_tests: list[str],
    regression_tests: list[str],
    container_image: str | None,
    timeout_seconds: int,
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []

    if container_image:
        from app.executor.docker_runner import docker_available

        if docker_available():
            checks.append(PreflightCheck(name="docker", ok=True))
        else:
            checks.append(
                PreflightCheck(
                    name="docker",
                    ok=False,
                    detail=(
                        "task requires container env but docker daemon is unavailable;"
                        " start Docker Desktop or run with execution_backend=local"
                    ),
                )
            )
    collects_on_host = True
    if container_image:
        # 容器环境:收集只能在容器内进行,宿主机 --collect-only 会给出误导性结论;
        # 预检只验证 daemon 可用性,双集收集交给引擎的受控执行(基线照常由引擎跑)
        collects_on_host = False
    else:
        import shutil

        exe_ok = bool(python_exe) and (
            Path(python_exe).exists() or shutil.which(python_exe) is not None
        )
        checks.append(
            PreflightCheck(
                name="interpreter",
                ok=exe_ok,
                detail="" if exe_ok else f"interpreter not found: {python_exe}",
            )
        )
        if exe_ok:
            probe = run_tests(
                [python_exe, "-m", "pytest", "--version", "-p", "no:cacheprovider"],
                workspace,
                min(timeout_seconds, 30),
            )
            checks.append(
                PreflightCheck(
                    name="pytest",
                    ok=probe.exit_code == 0,
                    detail="" if probe.exit_code == 0 else "pytest is not importable/usable",
                )
            )
        else:
            exe_ok = False

    if collects_on_host:
        checks.append(
            _collect_check(
                "collect_failed_set", python_exe, workspace, failed_tests, timeout_seconds
            )
        )
        checks.append(
            _collect_check(
                "collect_regression_set", python_exe, workspace, regression_tests, timeout_seconds
            )
        )

    return checks
