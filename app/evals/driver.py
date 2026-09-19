"""单任务驱动器:从 Bug 描述到最终报告的完整闭环(plain 引擎)。

M5 的 LangGraph 状态机会复用本模块的基线/验证逻辑;
判定规则唯一出处:4.3 的四个条件缺一不可。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import run_pytest
from app.config import get_settings
from app.errors import BudgetError, TaskCancelled, TaskError
from app.evals.pricing import estimate_cost
from app.evals.provenance import build_provenance
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
    outcome: str = ""  # graph 引擎的终局描述(resolved/failed/needs_review/...);plain 恒空(R2)
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
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_report(result: TaskResult, run_dir: Path) -> None:
    """落盘 report.json(N-17 整改):tmp + os.replace 原子写。

    /report 端点按"文件存在"判定就绪并被轮询;原地覆写时轮询方可能读到半截
    JSON 撞出 500。原子替换保证"存在即可完整读"。
    Windows 残留(R2 整改):轮询方恰持读句柄时 os.replace 抛 PermissionError,
    发生在任务收尾 finally 里会把已完成的任务吞成 NEEDS_REVIEW——退化为
    直接覆写(放弃原子性,保住报告)。
    """
    payload = json.dumps(result.as_dict(), ensure_ascii=False, indent=2)
    tmp = run_dir / "report.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.replace(tmp, run_dir / "report.json")
    except OSError:
        (run_dir / "report.json").write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)


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
    cancel_event: threading.Event | None = None,
) -> TaskResult:
    """执行一个任务:基线 → 工具循环 → 验证 → 判定,全程落盘。

    口径说明(R2 整改):plain 引擎是单轮多步循环,不消费 bug.max_rounds
    (那只在 graph 引擎的轮次语义里有意义);plain 的资源约束是
    max_turns + task_timeout_seconds + token_budget。

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
    # 批次溯源:任务开始即取证,后续任何崩溃路径的 report.json 都带复现口径
    result.provenance = build_provenance(result.model_provider, model_name, engine)
    tracker = Tracker(run_dir / "trajectory.jsonl", task_id=task_id)
    started = time.monotonic()
    ctx: ToolContext | None = None  # materialize 失败时 finally 仍可安全引用

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
            # P1-4 整改:资源上限此前只落在硬编码默认值上,settings 改了不生效
            max_read_lines=settings.max_read_lines,
            max_search_results=settings.max_search_results,
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

        # P2-3 整改:plain 引擎此前完全没有时间预算检查(graph 在 propose 前查),
        # 任务耗时不受 task_timeout 约束;进入工具循环前补一次门禁检查
        from app.graph.gates import ensure_budget

        ensure_budget(
            round_no=1,
            max_rounds=bug.max_rounds,
            tokens_used=0,
            token_budget=0,
            started_monotonic=started,
            time_budget_seconds=settings.task_timeout_seconds,
        )

        # LOCALIZE + PROPOSE_PATCH:工具循环(plain 引擎单轮多步)
        tracker.record(tool="start_loop", state="LOCALIZE", input_payload={"max_turns": max_turns})
        outcome = run_plain_loop(
            ctx,
            model,
            bug.issue_text,
            max_turns=max_turns,
            started_monotonic=started,
            time_budget_seconds=settings.task_timeout_seconds,
            cancel_event=cancel_event,
        )
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
        else:
            # 模型放弃/自认失败/静默结束,结局一致:VERIFY_FAILED(P3 死分支折叠)
            result.status, result.verdict = "VERIFY_FAILED", "failed"

    except BudgetError as exc:
        # N-11 整改:plain 引擎同样把循环已耗的 token/turns 记回账本
        result.status, result.verdict, result.error = "BUDGET_EXCEEDED", "failed", str(exc)
        result.turns = getattr(exc, "turns", 0)
        result.tokens_used = getattr(exc, "tokens_spent", 0)
        result.tokens_prompt = getattr(exc, "tokens_prompt", 0)
        result.tokens_completion = getattr(exc, "tokens_completion", 0)
    except TaskCancelled as exc:
        # 协作式取消:保留现场落盘;DB 状态由 cancel_task 置 CANCELLED,回写时让位
        result.status, result.verdict, result.error = "CANCELLED", "cancelled", str(exc)
    except TaskError as exc:
        result.status, result.verdict, result.error = "INVALID_TASK", "failed", str(exc)
    except Exception as exc:
        log.exception("task %s crashed", task_id)
        result.status, result.verdict, result.error = (
            "NEEDS_REVIEW",
            "needs_review",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # R3 整改:diff.patch 移入 finally——CANCELLED/崩溃/预算路径的工作区
        # 未被回滚,现场仍在,取证产物不应缺失
        try:
            (run_dir / "diff.patch").write_text(
                working_tree_diff(ctx.workspace).diff_text if ctx else "", encoding="utf-8"
            )
        except Exception:  # 落盘失败不影响任务结论
            log.exception("task %s: failed to write diff.patch", task_id)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.cost_usd = estimate_cost(
            result.model_name, result.tokens_prompt, result.tokens_completion
        )
        _write_report(result, run_dir)
        log.info(
            "task %s -> %s/%s (%sms)", task_id, result.status, result.verdict, result.duration_ms
        )

    return result
