"""单任务驱动器:从 Bug 描述到最终报告的完整闭环(plain 引擎)。

M5 的 LangGraph 状态机会复用本模块的基线/验证逻辑;
判定规则唯一出处:4.3 的四个条件缺一不可。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import run_pytest
from app.config import get_settings
from app.errors import BudgetError, TaskError
from app.evals.pricing import estimate_cost
from app.gitops.differ import working_tree_diff
from app.gitops.testing import materialize_repo
from app.graph.gates import run_gates
from app.graph.plain_loop import run_plain_loop
from app.llm.base import Model
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

log = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """一次任务的最终事实,report.json 的来源。"""

    task_id: str
    bug_id: str
    status: str = "CREATED"
    verdict: str = "needs_review"
    model_provider: str = ""
    model_name: str = ""  # 真实模型名(openai 时来自 Settings.llm_model;fake 为空)
    engine: str = "plain"
    rounds: int = 1
    turns: int = 0
    tokens_used: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cost_usd: float | None = None  # 约值;fake 或未收录模型为 None
    duration_ms: int = 0
    error: str | None = None
    gate_violations: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    baseline_failed: int = 0
    baseline_regression_ok: bool = False
    verify_failed_ok: bool = False
    verify_regression_ok: bool = False
    run_dir: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_report(result: TaskResult, run_dir: Path) -> None:
    (run_dir / "report.json").write_text(
        json.dumps(result.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_task(
    bug,  # BugTask
    model: Model,
    *,
    runs_root: Path,
    max_turns: int = 20,
    engine: str = "plain",
    task_id: str | None = None,
    run_dir: Path | None = None,
    model_name: str = "",
) -> TaskResult:
    """执行一个任务:基线 → 工具循环 → 验证 → 判定,全程落盘。

    task_id/run_dir 可由调用方(API 服务)指定,保证产物目录与服务记录一致。
    """
    settings = get_settings()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = task_id or f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
    # 绝对路径:junit 等报告若以相对路径传入,会被 pytest 相对 cwd 写进工作区,污染 diff
    run_dir = (Path(run_dir) if run_dir else Path(runs_root) / task_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = run_dir / "reports"

    result = TaskResult(
        task_id=task_id,
        bug_id=bug.id,
        model_provider=getattr(model, "provider", "unknown"),
        model_name=model_name,
        engine=engine,
        run_dir=str(run_dir),
    )
    tracker = Tracker(run_dir / "trajectory.jsonl", task_id=task_id)
    started = time.monotonic()

    try:
        # CREATED → BASELINE:bug 仓库是纯工作树,运行时物化为 git 仓库并固定基线 commit
        ws_dir = run_dir / "workspace"
        baseline_commit = materialize_repo(bug.repo_dir, ws_dir, extra_commit=False)
        ctx = ToolContext(
            task_id=task_id,
            workspace=ws_dir,
            baseline_commit=baseline_commit,
            tracker=tracker,
            report_dir=report_dir,
            test_sets=bug.test_sets,
            allowed_paths=bug.allowed_paths,
            max_patch_files=settings.max_patch_files,
            test_timeout_seconds=settings.test_timeout_seconds,
        )
        tracker.record(
            tool="create_workspace",
            state="BASELINE",
            input_payload={"baseline": baseline_commit[:12]},
        )

        failed_report, _ = run_pytest(
            ctx.python_exe, ctx.workspace, bug.failed_tests, report_dir / "baseline-failed.xml"
        )
        regression_report, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.regression_tests,
            report_dir / "baseline-regression.xml",
        )
        result.baseline_failed = failed_report.failed + failed_report.errors
        result.baseline_regression_ok = regression_report.all_passed

        if failed_report.all_passed:
            raise TaskError("baseline: failed_tests already pass; nothing to fix")
        if not regression_report.all_passed:
            raise TaskError("baseline: regression_tests not green; invalid bug")

        # LOCALIZE + PROPOSE_PATCH:工具循环(plain 引擎单轮多步)
        tracker.record(tool="start_loop", state="LOCALIZE", input_payload={"max_turns": max_turns})
        outcome = run_plain_loop(ctx, model, bug.issue_text, max_turns=max_turns)
        result.turns = outcome.turns
        result.tokens_used = outcome.tokens_used
        result.tokens_prompt = outcome.tokens_prompt
        result.tokens_completion = outcome.tokens_completion

        # VERIFY:用平台自己的执行器重新验证,不信任模型的声明
        verify_failed, _ = run_pytest(
            ctx.python_exe, ctx.workspace, bug.failed_tests, report_dir / "verify-failed.xml"
        )
        verify_regression, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.regression_tests,
            report_dir / "verify-regression.xml",
        )
        result.verify_failed_ok = verify_failed.all_passed
        result.verify_regression_ok = verify_regression.all_passed

        diff = working_tree_diff(ctx.workspace)
        result.changed_files = diff.changed_files
        gate = run_gates(
            diff.diff_text, allowed_paths=bug.allowed_paths, max_files=settings.max_patch_files
        )
        result.gate_violations = [str(v) for v in gate.violations]

        # 判定规则(企划书 4.3):四个条件同时满足才是 resolved
        if verify_failed.all_passed and verify_regression.all_passed and gate.ok:
            result.status, result.verdict = "FINISHED", "resolved"
        elif not gate.ok:
            result.status, result.verdict = "PATCH_REJECTED", "failed"
        elif outcome.finish_declared and not outcome.success:
            result.status, result.verdict = "VERIFY_FAILED", "failed"
        else:
            result.status, result.verdict = "VERIFY_FAILED", "failed"
        (run_dir / "diff.patch").write_text(diff.diff_text, encoding="utf-8")

    except BudgetError as exc:
        result.status, result.verdict, result.error = "BUDGET_EXCEEDED", "failed", str(exc)
    except TaskError as exc:
        result.status, result.verdict, result.error = "INVALID_TASK", "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - 驱动器必须把异常收敛为可复盘报告
        log.exception("task %s crashed", task_id)
        result.status, result.verdict, result.error = (
            "NEEDS_REVIEW",
            "needs_review",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.cost_usd = estimate_cost(
            result.model_name, result.tokens_prompt, result.tokens_completion
        )
        _write_report(result, run_dir)
        log.info(
            "task %s -> %s/%s (%sms)", task_id, result.status, result.verdict, result.duration_ms
        )

    return result
