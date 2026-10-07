"""graph 引擎驱动器:与 driver.run_task 同构,编排层换成 LangGraph。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.errors import TaskCancelled, TaskError
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
    model_name: str = "",
    cancel_event: threading.Event | None = None,
    branch_model_factory: Callable[[int], Model] | None = None,
) -> Any:
    """执行一个任务(状态机引擎);返回与 plain 引擎一致的 TaskResult。

    task_id/run_dir 可由调用方(API 服务)指定,保证产物目录与服务记录一致。
    branch_model_factory(卡5b):第 i 个候选用哪个模型。由调用方注入——graph 层
    不 import 测试替身,fake 回放的分支脚本读取留在 eval 入口;不传即恒不分支。
    """
    # P3-3 整改:第四入口(run_task_graph)此前零守卫——与 driver.run_task 同口径,
    # 真实模型(llm_enabled=True 且非 fake-replay)缺 model_name 时在物化任何
    # 仓库之前拒绝;fake 回放豁免(零花费路径不受约束,夜志 E2 明文理由)
    from app.config import get_settings
    from app.evals.provenance import require_model_name

    require_model_name(
        model_name,
        get_settings().llm_enabled and getattr(model, "provider", "") != "fake-replay",
    )
    from app.evals.driver import TaskResult  # 延迟导入,避免循环依赖
    from app.evals.pricing import estimate_cost
    from app.evals.provenance import build_provenance

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = task_id or f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir = (Path(run_dir) if run_dir else Path(runs_root) / task_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = run_dir / "reports"

    result = TaskResult(
        task_id=task_id,
        bug_id=bug.id,
        model_provider=getattr(model, "provider", "unknown"),
        model_name=model_name,
        engine="graph",
        run_dir=str(run_dir),
    )
    # 批次溯源:与 plain 引擎同口径,任务开始即取证
    result.provenance = build_provenance(
        result.model_provider, model_name, "graph", max_turns=max_turns
    )
    tracker = Tracker(run_dir / "trajectory.jsonl", task_id=task_id)
    started = time.monotonic()
    checkpointer = None
    preserved_diff: str | None = None

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
            cancel_event=cancel_event,
            branch_model_factory=branch_model_factory,
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
        # recursion_limit 随 max_rounds 推导(R2 整改):固定前缀 3 步(prepare/baseline/
        # localize),每轮 4 步(propose/apply/verify/rollback),成功收尾 1 步;
        # 硬编码 80 会在 max_rounds=20 的长重试任务耗尽轮数前误抛 GraphRecursionError
        config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": 4 * nodes.max_rounds + 8,
        }
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
        result.tokens_prompt = final.get("tokens_prompt", 0)
        result.tokens_completion = final.get("tokens_completion", 0)
        result.error = final.get("error")
        result.gate_violations = list(final.get("gate_violations", []))
        result.baseline_failed = final.get("baseline_failed", 0)
        result.baseline_regression_ok = final.get("baseline_regression_ok", False)
        result.verify_failed_ok = final.get("verify_failed_ok", False)
        result.verify_regression_ok = final.get("verify_regression_ok", False)
        result.changed_files = list(final.get("changed_files", []))
        # N-12 整改:末轮经 rollback 的任务,工作区已被 reset,
        # diff.patch 必须用 rollback 保全的现场,而不是回滚后的空 diff
        preserved_diff = final.get("preserved_diff")

    except TaskCancelled as exc:
        result.status = "CANCELLED"
        result.outcome = "cancelled"
        result.verdict = "cancelled"
        result.error = str(exc)
    except Exception as exc:
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
        # R3 整改:diff.patch 落盘移入 finally——CANCELLED/崩溃路径的现场
        # (工作区未被回滚)同样应留下取证产物
        try:
            if preserved_diff is not None:
                (run_dir / "diff.patch").write_text(preserved_diff, encoding="utf-8")
            else:
                (run_dir / "diff.patch").write_text(
                    working_tree_diff(run_dir / "workspace").diff_text, encoding="utf-8"
                )
        except Exception:  # 落盘失败不影响任务结论
            log.exception("task %s: failed to write diff.patch", task_id)
        # R2 整改:SqliteSaver 持有的是直接打开的 sqlite3 连接(见 checkpoint.py),
        # 每任务新建却不关闭会在长驻 API 进程中线性泄漏句柄,并锁住 run_dir 里的
        # checkpoints.sqlite(Windows 上长期占用)
        if checkpointer is not None:
            try:
                checkpointer.conn.close()
            except Exception:
                log.exception("failed to close checkpointer for task %s", task_id)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.cost_usd = estimate_cost(
            result.model_name, result.tokens_prompt, result.tokens_completion
        )
        # N-17 整改:与 plain 引擎同口径,tmp + os.replace 原子写,轮询方不会读到半截文件
        from app.evals.driver import _write_report

        _write_report(result, run_dir)
        log.info(
            "graph task %s -> %s/%s (%sms)",
            task_id,
            result.status,
            result.verdict,
            result.duration_ms,
        )

    return result
