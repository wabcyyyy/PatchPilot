"""pytest 适配器:组装命令、执行、解析 JUnit XML 报告。

首版唯一适配器;接口(report 结构、命令组装)为 Maven/Jest 预留,
核心状态机不感知"pytest"字样。
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.errors import ExecError
from app.executor.local_runner import TestRunResult, run_tests

log = logging.getLogger(__name__)

# pytest 退出码:0 成功,1 用例失败,2 被中断,3 内部错误,4 用法错误,5 未收集到。
# 判定只依赖 RC_OK 与 RC_NO_TESTS_COLLECTED 两个常量,其余以注释存档。
RC_OK = 0
RC_NO_TESTS_COLLECTED = 5


@dataclass
class FailedCase:
    """单个失败用例及其归一化签名。"""

    test_name: str
    test_id: str
    kind: str  # failure | error
    message_first_line: str
    signature: str
    # 模型可见层用的完整 traceback(junit <failure> 节点正文,截 4000 字符)。
    # 放在末位且带默认值:既有位置参数构造与签名判定口径都不受影响。
    traceback: str = ""


@dataclass
class PytestReport:
    """一次 pytest 执行的结构化报告。"""

    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0
    duration_ms: int = 0
    timed_out: bool = False
    no_tests_collected: bool = False
    failed_cases: list[FailedCase] = field(default_factory=list)
    requested_ids: list[str] = field(default_factory=list)
    case_results: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """判定口径(P0-1 整改):请求的每条测试 id 都**真实跑过并通过**。

        仅凭 exit_code==0 防不了伪:仓库内 conftest 可以把用例 skip 或
        deselect,junit 的 rc 依然是 0。因此:rc==0 且零失败/错误/跳过之外,
        还要求每条 requested_id 都能在 junit 的 testcase 中匹配到一条
        status=passed 的记录。无期望 id 的调用方退化为"零失败零跳过"。
        """
        if self.exit_code != RC_OK or self.timed_out:
            return False
        if self.failed or self.errors or self.skipped:
            return False
        if not self.requested_ids:
            return True
        remaining = set(self.requested_ids)
        for file_attr, classname, case_name, status in self.case_results:
            if status != "passed":
                continue
            for rid in list(remaining):
                if _matches_requested(file_attr, classname, case_name, rid):
                    remaining.discard(rid)
        return not remaining


def build_pytest_cmd(
    python_exe: str,
    test_ids: list[str] | None = None,
    junit_xml: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """组装 pytest 命令(参数列表,供白名单与 runner 使用)。

    junit_family 强制 xunit1:testcase 必须携带 file 属性——all_passed 的
    期望 id 匹配以 file 为主判据(xunit2 不写 file,classname 随 rootdir 漂移)。
    """
    cmd = [python_exe, "-m", "pytest", "-q", "--color=no", "-o", "junit_family=xunit1"]
    if junit_xml is not None:
        cmd.append(f"--junitxml={junit_xml.as_posix()}")
    if extra_args:
        cmd.extend(extra_args)
    if test_ids:
        cmd.extend(test_ids)
    return cmd


def failure_signature(kind: str, message: str) -> str:
    """归一化失败签名:同一失败在多轮之间保持稳定,才能比较"是否还是同一个失败"。

    规则:取消息首行,压缩空白,截断到 160 字符;附失败类别。
    """
    first_line = message.strip().splitlines()[0] if message.strip() else "(no message)"
    normalized = " ".join(first_line.split())[:160]
    return f"{kind}: {normalized}"


def parse_junit_xml(path: Path) -> PytestReport:
    """解析 pytest 生成的 JUnit XML。"""
    report = PytestReport(exit_code=RC_OK)
    if not path.exists():
        report.errors = 1
        report.failed_cases.append(
            FailedCase(
                "(report)", "(report)", "error", "junit xml not generated", "error: no junit xml"
            )
        )
        return report

    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    for suite in suites:
        report.collected += int(suite.get("tests", "0"))
        report.errors += int(suite.get("errors", "0"))
        report.failed += int(suite.get("failures", "0"))
        report.skipped += int(suite.get("skipped", "0"))
        report.passed += report.collected - report.errors - report.failed - report.skipped
        for case in suite.iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            status = "passed"
            node = None
            if failure is not None:
                status, node = "failure", failure
            elif error is not None:
                status, node = "error", error
            elif skipped is not None:
                status = "skipped"
            report.case_results.append(
                (case.get("file", ""), case.get("classname", ""), case.get("name", "?"), status)
            )
            if node is None:
                continue
            kind = "failure" if failure is not None else "error"
            message = node.get("message", "") or (node.text or "")
            traceback_text = (node.text or "")[:4000]
            test_id = _case_id(case)
            report.failed_cases.append(
                FailedCase(
                    test_name=case.get("name", "?"),
                    test_id=test_id,
                    kind=kind,
                    message_first_line=message.strip().splitlines()[0] if message.strip() else "",
                    signature=failure_signature(kind, message),
                    traceback=traceback_text,
                )
            )
    return report


def _matches_requested(file_attr: str, classname: str, case_name: str, requested: str) -> bool:
    """junit 的 testcase 是否对应请求的 node id。

    主判据是 junit 自带的 file 属性(命令已强制 junit_family=xunit1 保证其存在):
    请求 id 的文件路径段与 file 做后缀匹配——classname 相对内层 rootdir,同一仓库
    在不同 rootdir 下会得出不同 classname,不可靠。file 缺失时退化为 classname 尾部
    与"模块点分路径(+类链)"的后缀匹配。
    """
    parts = requested.replace("\\", "/").split("::")
    if case_name != parts[-1]:
        return False
    req_path = parts[0]
    file_n = file_attr.replace("\\", "/")
    if file_n:
        return file_n == req_path or file_n.endswith("/" + req_path)
    full = req_path.removesuffix(".py").replace("/", ".")
    class_chain = ".".join(parts[1:-1])
    expected_tail = f"{full}.{class_chain}" if class_chain else full
    return classname == expected_tail or classname.endswith("." + expected_tail)


def _case_id(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def run_pytest(
    python_exe: str,
    cwd: Path | str,
    test_ids: list[str] | None = None,
    report_path: Path | None = None,
    timeout_seconds: int | None = None,
    extra_args: list[str] | None = None,
) -> tuple[PytestReport, TestRunResult]:
    """执行 pytest 并解析报告;报告缺失/超时都反映在返回值里。

    basetemp 显式指向报告目录:目标仓库测试里的 tmp_path fixture 不再依赖
    系统临时目录(权限/容量不可控),也不污染被验证的工作区。
    execution_backend="docker" 时改在临时容器内执行(隔离边界见 docker_runner),
    签名与返回结构不变,上层无感知;镜像需预装 pytest(见 docker/executor.Dockerfile)。
    """
    settings = get_settings()
    timeout = timeout_seconds or settings.test_timeout_seconds
    junit = report_path or (Path(cwd) / ".patchpilot_junit.xml")
    if settings.execution_backend == "docker":
        return _run_pytest_in_container(cwd, test_ids, junit, timeout)
    cmd = build_pytest_cmd(python_exe, test_ids, junit, extra_args)
    cmd.append(f"--basetemp={(junit.parent / 'basetemp').as_posix()}")
    run = run_tests(cmd, cwd, timeout)
    report = parse_junit_xml(junit)
    report.requested_ids = list(test_ids or [])
    report.exit_code = run.exit_code
    report.duration_ms = run.duration_ms
    report.timed_out = run.timed_out
    report.no_tests_collected = run.exit_code == RC_NO_TESTS_COLLECTED and not report.timed_out
    log.info(
        "pytest: rc=%s passed=%s failed=%s errors=%s (timed_out=%s)",
        report.exit_code,
        report.passed,
        report.failed,
        report.errors,
        report.timed_out,
    )
    return report, run


def _run_pytest_in_container(
    cwd: Path | str,
    test_ids: list[str] | None,
    junit: Path,
    timeout_seconds: int,
) -> tuple[PytestReport, TestRunResult]:
    """docker 后端的 pytest 执行:复用 docker_runner 的双挂载与 junit 回传。

    与 local 路径的差异:extra_args 不下发(容器内命令由 docker_runner 组装,
    当前生产调用方未使用该参数);basetemp 用容器内可弃临时目录。
    """
    from app.executor.docker_runner import docker_available, run_tests_in_container

    if not docker_available():
        raise ExecError("execution_backend='docker' but docker daemon is not available")
    report, run = run_tests_in_container(
        cwd, test_ids or [], report_dir=junit.parent, timeout_seconds=timeout_seconds
    )
    report.no_tests_collected = run.exit_code == RC_NO_TESTS_COLLECTED and not run.timed_out
    log.info(
        "pytest(容器内): rc=%s passed=%s failed=%s errors=%s (timed_out=%s)",
        report.exit_code,
        report.passed,
        report.failed,
        report.errors,
        report.timed_out,
    )
    return report, run
