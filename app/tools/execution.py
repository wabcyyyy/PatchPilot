"""测试执行工具:Agent 只能跑 manifest 预定义的测试集,不能任意指定用例。"""

from __future__ import annotations

import logging
import uuid

from app.adapters.pytest_adapter import build_pytest_cmd, run_pytest
from app.graph.gates import ensure_command_allowed
from app.tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

VALID_SETS = ("failed", "regression", "all")


def _resolve_ids(ctx: ToolContext, test_set: str) -> list[str]:
    if test_set == "all":
        return list(ctx.test_sets.get("failed", [])) + list(ctx.test_sets.get("regression", []))
    return list(ctx.test_sets.get(test_set, []))


def run_tests(ctx: ToolContext, test_set: str = "all") -> ToolResult:
    """执行预定义测试集之一,返回结构化结果(失败用例 + 归一化签名)。

    junit 报告写到 report_dir(工作区外),不污染 Agent 的 diff 视图。
    """
    test_set = (test_set or "all").strip().lower()
    if test_set not in VALID_SETS:
        return ToolResult.fail(f"unknown test_set {test_set!r}; valid: {VALID_SETS}")
    ids = _resolve_ids(ctx, test_set)
    if not ids:
        return ToolResult.fail(f"test_set {test_set!r} is empty for this task")

    junit_path = ctx.report_dir / f"junit-{uuid.uuid4().hex}.xml"
    cmd = build_pytest_cmd(ctx.python_exe, ids, junit_path)
    ensure_command_allowed(cmd, ctx.whitelist)

    report, _ = run_pytest(
        ctx.python_exe,
        ctx.workspace,
        test_ids=ids,
        report_path=junit_path,
        timeout_seconds=ctx.test_timeout_seconds,
    )
    return ToolResult(
        ok=True,
        output={
            "test_set": test_set,
            "exit_code": report.exit_code,
            "passed": report.passed,
            "failed": report.failed,
            "errors": report.errors,
            "timed_out": report.timed_out,
            "all_passed": report.all_passed,
            "failed_cases": [
                {"name": c.test_name, "test_id": c.test_id, "signature": c.signature}
                for c in report.failed_cases
            ],
        },
    )
