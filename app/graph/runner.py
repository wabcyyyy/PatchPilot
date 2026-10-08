"""graph 引擎驱动器:与 driver.run_task 同构,编排层换成 LangGraph。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.errors import TaskCancelled, TaskError
from app.gitops.differ import working_tree_diff
from app.graph.builder import build_graph
from app.graph.checkpoint import make_sqlite_checkpointer
from app.graph.nodes import TaskNodes
from app.graph.resume import prepare_resume
from app.graph.state import TaskState
from app.llm.base import Model
from app.tools.tracker import Tracker

if TYPE_CHECKING:  # 复盘 R-2:运行时零导入成本,类型标注兑现 AGENTS"全量标注"
    from app.evals.bugset import BugTask

log = logging.getLogger(__name__)


def run_task_graph(
    bug: BugTask,
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
    resume: bool = False,
) -> Any:
    """执行一个任务(状态机引擎);返回与 plain 引擎一致的 TaskResult。

    task_id/run_dir 可由调用方(API 服务)指定,保证产物目录与服务记录一致。
    branch_model_factory(卡5b):第 i 个候选用哪个模型。由调用方注入——graph 层
    不 import 测试替身,fake 回放的分支脚本读取留在 eval 入口;不传即恒不分支。

    resume=True(M6 崩溃恢复):按 `thread_id = task_id` 读回 SqliteSaver 检查点,
    从检查点记录的 `next` 节点续跑,并把被打断阶段的 LoopSnapshot 交给该阶段
    (见 app/graph/resume.py 与 app/graph/loop_state.py 的分工)。三个前提:
    ①可写阶段续跑前工作区必须先复位到基线补丁锚点才算数(resume_reset_workspace 事件);
    ②**门禁一律重跑**——恢复只回升阶时的"工作记忆",绝不复用崩溃前的任何判断:
      apply 门禁 / verify 双测试集 / E3 双跑一致性 / 最终 verdict 全部在本次续跑里
      真实执行一遍(测试按轨迹事件计数钉住这一条);
    ③token 与轮次不双计:累计量随快照与检查点带回,阶段轮次上限不重授。
    无可续位置(无检查点/无快照且图已终态)时如实回退冷启动,并记 resume_unavailable。
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
        # recursion_limit 随 max_rounds 推导(R2 整改;M5 起 5N+8)。斜率不靠推算:满轮数
        # 重试路径实测每轮 5 个 superstep、固定段 3 个(N=2/3/6 → 13/18/33,三点共线,
        # 见 tests/test_plan_invariants.py 的实测用例——往环里加节点会先把那条钉红)。
        # 于是余量恒为 5 步、与轮数无关:抬 max_rounds 不会失守;而旧公式 4N+8 在 PLAN
        # 插入后会在轮数用满前先撞上 LangGraph 递归上限——那是崩溃(GraphRecursionError
        # 被 runner 收敛成 NEEDS_REVIEW),不是我们自己的结构化终态 BUDGET_EXCEEDED,
        # 判定语义会凭空变成"需要人看";硬编码 80 同理会在 max_rounds=20 的长任务上更早触发。
        config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": 5 * nodes.max_rounds + 8,
        }
        # M6 恢复分支:检查点里有被打断的节点 → 从那里 invoke(None) 续跑;
        # prepare_resume 返回 None(无检查点/已终态/复位失败)→ 如实回退冷启动
        resume_config = (
            prepare_resume(nodes, graph, run_dir=run_dir, config=config, task_id=task_id)
            if resume
            else None
        )
        if resume_config is not None:
            final: TaskState = graph.invoke(None, config=resume_config)  # type: ignore[assignment]
        else:
            final = graph.invoke(initial, config=config)  # type: ignore[assignment]

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
