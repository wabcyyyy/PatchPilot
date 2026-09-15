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
from app.executor.local_runner import TestRunResult, run_tests

log = logging.getLogger(__name__)

# pytest 退出码含义(用于判定,不依赖输出文本)
RC_OK = 0
RC_TESTS_FAILED = 1
RC_INTERRUPTED = 2
RC_INTERNAL_ERROR = 3
RC_USAGE_ERROR = 4
RC_NO_TESTS_COLLECTED = 5


@dataclass
class FailedCase:
    """单个失败用例及其归一化签名。"""

    test_name: str
    test_id: str
    kind: str  # failure | error
    message_first_line: str
    signature: str


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

    @property
    def all_passed(self) -> bool:
        return self.exit_code == RC_OK and not self.timed_out


def build_pytest_cmd(
    python_exe: str,
    test_ids: list[str] | None = None,
    junit_xml: Path | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """组装 pytest 命令(参数列表,供白名单与 runner 使用)。"""
    cmd = [python_exe, "-m", "pytest", "-q", "--color=no"]
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
            node = failure if failure is not None else error
            if node is None:
                continue
            kind = "failure" if failure is not None else "error"
            message = node.get("message", "") or (node.text or "")
            test_id = _case_id(case)
            report.failed_cases.append(
                FailedCase(
                    test_name=case.get("name", "?"),
                    test_id=test_id,
                    kind=kind,
                    message_first_line=message.strip().splitlines()[0] if message.strip() else "",
                    signature=failure_signature(kind, message),
                )
            )
    return report


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
    """
    junit = report_path or (Path(cwd) / ".patchpilot_junit.xml")
    cmd = build_pytest_cmd(python_exe, test_ids, junit, extra_args)
    cmd.append(f"--basetemp={(junit.parent / 'basetemp').as_posix()}")
    run = run_tests(cmd, cwd, timeout_seconds or get_settings().test_timeout_seconds)
    report = parse_junit_xml(junit)
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
