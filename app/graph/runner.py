"""graph 引擎驱动器:与 driver.run_task 同构,编排层换成 LangGraph。"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.errors import TaskError
from app.gitops.differ import working_tree_diff
from app.graph.builder import build_graph
from app.graph.checkpoint import make_sqlite_checkpointer
from app.graph.nodes import TaskNodes
from app.graph.state import TaskState
from app.llm.base import Model
from app.tools.tracker import Tracker

log = logging.getLogger(__name__)


def run_task_graph(
    bug,
    model: Model,
    *,
    runs_root: Path,
    max_rounds: int | None = None,
    max_turns: int = 20,
    use_checkpoint: bool = True,
    task_id: str | None = None,
    run_dir: Path | None = None,
) -> Any:
    """执行一个任务(状态机引擎);返回与 plain 引擎一致的 TaskResult。

    task_id/run_dir 可由调用方(API 服务)指定,保证产物目录与服务记录一致。
    """
    from app.evals.driver import TaskResult  # 延迟导入,避免循环依赖

    settings = get_settings()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = task_id or f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir = (Path(run_dir) if run_dir else Path(runs_root) / task_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = run_dir / "reports"

    result = TaskResult(
        task_id=task_id,
        bug_id=bug.id,
        model_provider=getattr(model, "provider", "unknown"),
        engine="graph",
        run_dir=str(run_dir),
    )
    tracker = Tracker(run_dir / "trajectory.jsonl", task_id=task_id)
    started = time.monotonic()

    try:
        nodes = TaskNodes(
            bug=bug,
            model=model,
            workspace=run_dir / "workspace",
            tracker=tracker,
            report_dir=report_dir,
            max_rounds=max_rounds if max_rounds is not None else bug.max_rounds,
            max_turns=max_turns,
            started_monotonic=started,
        )
        checkpointer = (
            make_sqlite_checkpointer(run_dir / "checkpoints.sqlite") if use_checkpoint else None
        )
        graph = build_graph(nodes, checkpointer=checkpointer)

        initial: TaskState = {
            "bug_id": bug.id,
            "issue_text": bug.issue_text,
            "failed_tests": bug.failed_tests,
            "regression_tests": bug.regression_tests,
            "allowed_paths": bug.allowed_paths,
            "max_rounds": nodes.max_rounds,
            "status": "CREATED",
            "round_no": 1,
            "turns": 0,
            "tokens_used": 0,
        }
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 80}
        final: TaskState = graph.invoke(initial, config=config)  # type: ignore[assignment]

        result.status = final.get("status", "NEEDS_REVIEW")
        outcome = final.get("outcome")
        if outcome is None:
            outcome = "resolved" if final.get("status") == "FINISHED" else "failed"
        result.outcome = outcome
        result.verdict = outcome if outcome in {"resolved", "needs_review"} else "failed"
        result.rounds = max(1, final.get("round_no", 1))
        result.turns = final.get("turns", 0)
        result.tokens_used = final.get("tokens_used", 0)
        result.error = final.get("error")
        result.gate_violations = list(final.get("gate_violations", []))
        result.baseline_failed = final.get("baseline_failed", 0)
        result.baseline_regression_ok = final.get("baseline_regression_ok", False)
        result.verify_failed_ok = final.get("verify_failed_ok", False)
        result.verify_regression_ok = final.get("verify_regression_ok", False)
        result.changed_files = list(final.get("changed_files", []))

        diff = working_tree_diff(run_dir / "workspace")
        (run_dir / "diff.patch").write_text(diff.diff_text, encoding="utf-8")

    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, TaskError):
            result.status, result.outcome, result.verdict, result.error = (
                "INVALID_TASK",
                "invalid",
                "failed",
                str(exc),
            )
        else:
            log.exception("graph task %s crashed", task_id)
            result.status = "NEEDS_REVIEW"
            result.outcome = "needs_review"
            result.verdict = "needs_review"
            result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        (run_dir / "report.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(
            "graph task %s -> %s/%s (%sms)",
            task_id,
            result.status,
            result.verdict,
            result.duration_ms,
        )

    return result
