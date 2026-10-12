"""单任务驱动器:从 Bug 描述到最终报告的完整闭环(plain 引擎)。

graph 与 plain 保留不同编排,复用工具循环、pytest 适配器和严格 final_acceptance。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import run_pytest
from app.agent.runtime import TaskRuntime
from app.agent.session import SessionState
from app.config import get_settings
from app.context.task_memory import TaskMemory, load_memory_state
from app.errors import BudgetError, TaskCancelled, TaskError
from app.evals.bugset import BugTask
from app.evals.pricing import estimate_cost
from app.evals.provenance import (
    build_provenance,
    config_snapshot,
    dirty_fingerprint,
    git_commit,
    missing_inputs_at_commit,
    require_model_name,
    worktree_dirty,
)
from app.gitops.differ import working_tree_diff
from app.gitops.testing import materialize_repo
from app.graph.gates import run_gates
from app.graph.plain_loop import LoopOutcome, run_plain_loop
from app.llm.base import Model
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

log = logging.getLogger(__name__)

# 执行体签名:agent 循环的可替换插槽(消融对照臂用它换掉循环形状,不动判定路径)
AgentFn = Callable[..., LoopOutcome]


def _default_agent(
    ctx: ToolContext,
    model: Model,
    bug: Any,
    **kwargs: Any,
) -> LoopOutcome:
    """默认执行体:plain 引擎的单轮多步工具循环(历史行为,一字未变)。"""
    return run_plain_loop(ctx, model, bug.issue_text, **kwargs)


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
    # 执行体:agent=默认工具循环;one_shot=消融对照臂(见 app/evals/single_shot.py)。
    # 与 engine 分开记,因为两臂跑的是同一个引擎,区别只在循环形状。
    arm: str = "agent"
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
    # S02(spec §3.1):报告结构版本与三个解释维度。默认值按"未验证/未知"诚实落盘,
    # 旧报告缺字段时读方也显示 not_run/unknown,不冒充已验证或额度合规。
    report_schema_version: int = 2
    validation_status: str = "not_run"  # not_run|passed|failed|inconclusive
    gate_status: str = "not_run"  # not_run|passed|rejected|inconclusive
    resource_status: str = "unknown"  # within_budget|exhausted|exceeded|unknown
    # G03:账本摘要(预算模式、各维度上限/已用/在途预留/未知持有/费用来源标签)。
    # 旧报告没这个键,读方一律 .get() 取值,不冒充"合规"或"已结算"。
    resources: dict[str, Any] = field(default_factory=dict)
    # S03(ADR-0009 §4):成功绑定当前候选与当前验证尝试的证据链
    candidate_id: str = ""
    candidate_hash: str = ""
    verification_attempt_id: str = ""
    # G13:子任务贡献摘要(role/subtask_id/ok/termination/计量/候选引用/错误)。
    # 报告逐条列出,不以"线程池存在"充当完成证据(SPEC G13 退出条件)。
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    # G16:report v2 补全段(验收清单/候选/命令/输入事件/运行身份),收尾时从 run_dir
    # 工件收集(report_evidence.collect_run_evidence)。旧报告缺该键 ⇒ 读方不渲染,
    # 不冒充"没有验收/没有候选";收集失败落空结构,不拦报告本身。
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _DeadlineReached(Exception):
    """S02b 内部哨兵:验证段撞任务级 deadline,带着已得的部分证据跳进 finally。
    单独列出以免落进 except Exception 的 NEEDS_REVIEW 分支。"""


def _write_report(result: TaskResult, run_dir: Path) -> None:
    """落盘 report.json(N-17 整改):tmp + os.replace 原子写。

    /report 端点按"文件存在"判定就绪并被轮询;原地覆写时轮询方可能读到半截
    JSON 撞出 500。原子替换保证"存在即可完整读"。
    Windows 残留(R2 整改):轮询方恰持读句柄时 os.replace 抛 PermissionError,
    发生在任务收尾 finally 里会把已完成的任务吞成 NEEDS_REVIEW——退化为
    直接覆写(放弃原子性,保住报告)。
    """
    # G16:report v2 补全段(验收清单/候选/命令/输入事件/运行身份)在两引擎共用的
    # 这一个落盘点收集;收集失败在 collect 内部落空结构,这里再兜一层 —— 证据补全
    # 是附加物,任何意外都不得把已完成任务吞成 NEEDS_REVIEW。
    try:
        from app.evals.report_evidence import collect_run_evidence

        result.evidence = collect_run_evidence(run_dir)
    except Exception:
        log.exception("task %s: run evidence collection failed", result.task_id)
    payload = json.dumps(result.as_dict(), ensure_ascii=False, indent=2)
    tmp = run_dir / "report.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.replace(tmp, run_dir / "report.json")
    except OSError:
        (run_dir / "report.json").write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)


def run_task(
    bug: BugTask,
    model: Model,
    *,
    runs_root: Path,
    max_turns: int = 20,
    engine: str = "plain",
    task_id: str | None = None,
    run_dir: Path | None = None,
    model_name: str = "",
    cancel_event: threading.Event | None = None,
    arm: str = "agent",
    agent: AgentFn | None = None,
    tracker_sink: Callable[[dict[str, Any]], None] | None = None,
) -> TaskResult:
    """执行一个任务:基线 → 工具循环 → 验证 → 判定,全程落盘。

    口径说明(R2 整改):plain 引擎是单轮多步循环,不消费 bug.max_rounds
    (那只在 graph 引擎的轮次语义里有意义);plain 的资源约束是
    max_turns + task_timeout_seconds + token_budget。

    task_id/run_dir 可由调用方(API 服务)指定,保证产物目录与服务记录一致。

    agent 是执行体插槽:默认跑 plain 工具循环,消融对照臂传入自己的循环。
    **只有循环可换**——基线、验证、门禁、判定规则对本臂与默认臂逐字同一条路径,
    否则两臂之差就说不清是机制贡献还是判定口径差异。
    """
    settings = get_settings()
    provider = getattr(model, "provider", "unknown")
    # E2 fail-fast:真实模型(llm_enabled=True 且非 fake 回放)缺 model_name 时,
    # 在物化任何仓库、发起任何任务之前拒绝——花了钱生不出可信记录是最坏结局
    require_model_name(model_name, settings.llm_enabled and provider != "fake-replay")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = task_id or f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
    # 绝对路径:junit 等报告若以相对路径传入,会被 pytest 相对 cwd 写进工作区,污染 diff
    run_dir = (Path(run_dir) if run_dir else Path(runs_root) / task_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_dir = run_dir / "reports"

    result = TaskResult(
        task_id=task_id,
        bug_id=bug.id,
        model_provider=provider,
        model_name=model_name,
        engine=engine,
        arm=arm,
        run_dir=str(run_dir),
    )
    # 批次溯源:任务开始即取证,后续任何崩溃路径的 report.json 都带复现口径。
    # 策略登记面(experiment_policy)只在 graph 引擎被真实消费(S10a 的两臂都在 graph),
    # plain 路径不伪造一份"看起来注册过"的策略,protocol_identity 记 None。
    tracker = Tracker(run_dir / "trajectory.jsonl", task_id=task_id, sink=tracker_sink)
    started = time.monotonic()
    # S02b:任务级墙钟截止时刻,执行线程启动时建立(排队不计);plain 验证段
    # 从此与 graph 同口径——每次 run_pytest 前后复查,剩余时间不足不启动下一次
    execution_started_epoch = time.time()
    ctx: ToolContext | None = None  # materialize 失败时 finally 仍可安全引用
    # S02/F1:任务级资源账本(与 graph 引擎同一套 ResourceLedger)
    from app.graph.resources import ledger_from_settings

    # G07:plain 引擎与 graph 共用同一份**冻结的**运行 profile。同一个 profile 名字在两条
    # 路上必须给出同一组数字(软窗口/尾部钉住/容量判据),否则"换 profile"就成了看引擎才
    # 生效的话;消融臂换 profile 也就是换这一份读数。
    # G16 收口 G07:服务受理路径在 run_dir 落了冻结契约 —— 运行 profile 与工具上限从它
    # 复原,受理与执行之间的环境漂移改变不了已受理任务的口径;直接驱动(eval CLI)没有
    # 契约文件时按现状构建/读取,兼容约定不变。
    frozen_policy: dict[str, Any] | None = None
    spec_file = run_dir / "task_spec.json"
    if spec_file.is_file():
        from app.task_spec import TaskSpec, settings_for_policy

        try:
            accepted = TaskSpec.read_file(spec_file)
        except (ValueError, OSError) as exc:
            raise TaskError(f"accepted task_spec unreadable: {exc}") from exc
        frozen_policy = accepted.effective_policy
        settings = settings_for_policy(settings, frozen_policy)
        max_turns = accepted.max_turns
    runtime = TaskRuntime.from_frozen(frozen_policy) or TaskRuntime.build(settings)
    ledger = ledger_from_settings(
        task_id=task_id,
        settings=settings,
        price_model=model_name or str(getattr(settings, "llm_model", "") or ""),
        run_dir=run_dir,
    )
    deadline_epoch = (
        execution_started_epoch + settings.task_timeout_seconds
        if settings.task_timeout_seconds > 0
        else None
    )

    result.provenance = build_provenance(
        result.model_provider, model_name, engine, arm, max_turns=max_turns, settings=settings
    )

    def _policy_value(key: str) -> Any:
        """与 graph 侧 TaskNodes._policy_value 同一口径:冻结策略优先,Settings 兜底。"""
        if frozen_policy is not None and key in frozen_policy:
            return frozen_policy[key]
        return getattr(settings, key)

    def _time_overrun() -> str | None:
        if deadline_epoch is not None and time.time() > deadline_epoch:
            return f"verify: task exceeded time budget (deadline {int(deadline_epoch)} passed)"
        return None

    def _deadline_terminal(message: str) -> None:
        from app.graph.resources import EXHAUSTED

        result.status, result.verdict, result.error = "BUDGET_EXCEEDED", "failed", message
        result.resource_status = EXHAUSTED

    try:
        # CREATED → BASELINE:bug 仓库是纯工作树,运行时物化为 git 仓库并固定基线 commit
        ws_dir = run_dir / "workspace"
        baseline_commit = materialize_repo(bug.repo_dir, ws_dir, extra_commit=False)
        # G06:plain 引擎与 graph 同一口径 —— 会话身份与任务记忆由 ctx 带给工具层。
        # 恢复时优先读回 run_dir 里那一份(观测是崩溃前真实发生过的,不是重新推断)。
        memory = None
        session = None
        if getattr(settings, "task_memory_enabled", False):
            memory = load_memory_state(run_dir) or TaskMemory(task_id)
            session = SessionState(task_id, memory.session_id)
            memory.session_id = session.session_id
        ctx = ToolContext(
            task_id=task_id,
            workspace=ws_dir,
            baseline_commit=baseline_commit,
            tracker=tracker,
            report_dir=report_dir,
            env=bug.env,
            test_sets=bug.test_sets,
            allowed_paths=bug.allowed_paths,
            max_patch_files=int(_policy_value("max_patch_files")),
            test_timeout_seconds=int(_policy_value("test_timeout_seconds")),
            # P1-4 整改:资源上限此前只落在硬编码默认值上,settings 改了不生效
            max_read_lines=int(_policy_value("max_read_lines")),
            max_search_results=int(_policy_value("max_search_results")),
            memory=memory,
            session=session,
            execution_backend=settings.execution_backend,
            cancel_event=cancel_event,
        )
        tracker.record(
            tool="create_workspace",
            state="BASELINE",
            input_payload={"baseline": baseline_commit[:12]},
        )
        # 与 graph 同一件事(见 nodes.prepare 的同名事件):plain/one-shot 臂也要在轨迹里
        # 留下"这一跑用的是哪套窗口/轮数/容量口径",否则两臂对照少一边读数。
        tracker.record(
            tool="runtime_profile",
            state="BASELINE",
            input_payload={"profile_name": runtime.profile_name},
            output_summary=runtime.as_dict(),
        )

        if deadline_epoch is not None and time.time() > deadline_epoch:
            _deadline_terminal("baseline: task exceeded time budget (deadline passed)")
            raise _DeadlineReached()
        failed_report, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.failed_tests,
            report_dir / "baseline-failed.xml",
            env=ctx.env,
            timeout_seconds=ctx.test_timeout_seconds,
            execution_backend=ctx.execution_backend or None,
        )
        regression_report, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.regression_tests,
            report_dir / "baseline-regression.xml",
            env=ctx.env,
            timeout_seconds=ctx.test_timeout_seconds,
            execution_backend=ctx.execution_backend or None,
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
            deadline_epoch=deadline_epoch,
        )

        # LOCALIZE + PROPOSE_PATCH:工具循环(plain 引擎单轮多步)
        tracker.record(
            tool="start_loop",
            state="LOCALIZE",
            input_payload={"max_turns": max_turns, "arm": arm},
        )
        outcome = (agent or _default_agent)(
            ctx,
            model,
            bug,
            max_turns=max_turns,
            deadline_epoch=deadline_epoch,
            cancel_event=cancel_event,
            ledger=ledger,
            token_budget=settings.token_budget,
            **runtime.loop_settings(),
        )
        result.turns = outcome.turns
        result.tokens_used = outcome.tokens_used
        result.tokens_prompt = outcome.tokens_prompt
        result.tokens_completion = outcome.tokens_completion

        # VERIFY:用平台自己的执行器重新验证,不信任模型的声明。
        # S02b:每次执行前查剩余时间(不足不启动下一次),执行后复查——
        # 越时即 BUDGET_EXCEEDED 结构化收尾,已拿到的部分证据随状态保留
        if (overrun := _time_overrun()) is not None:
            _deadline_terminal(overrun)
            raise _DeadlineReached()  # 跳过后续验证段,进 finally 落盘
        verify_failed, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.failed_tests,
            report_dir / "verify-failed.xml",
            env=ctx.env,
            timeout_seconds=ctx.test_timeout_seconds,
            execution_backend=ctx.execution_backend or None,
        )
        result.verify_failed_ok = verify_failed.all_passed
        if (overrun := _time_overrun()) is not None:
            _deadline_terminal(overrun)  # failed 集的部分证据已在 result 上
            raise _DeadlineReached()
        verify_regression, _ = run_pytest(
            ctx.python_exe,
            ctx.workspace,
            bug.regression_tests,
            report_dir / "verify-regression.xml",
            env=ctx.env,
            timeout_seconds=ctx.test_timeout_seconds,
            execution_backend=ctx.execution_backend or None,
        )
        result.verify_regression_ok = verify_regression.all_passed
        if (overrun := _time_overrun()) is not None:
            _deadline_terminal(overrun)
            raise _DeadlineReached()
        # E3 双跑(S02:两引擎同一条复核规则,比较臂不允许少跑):第一遍双集全绿
        # (即将判 resolved)时同命令重跑并比对;不一致 = inconclusive,不 resolved
        from app.graph.acceptance import double_run_mismatch, final_acceptance

        mismatch = ""
        if verify_failed.all_passed and verify_regression.all_passed and settings.verify_double_run:
            # 双跑是 verify 段最贵的追加开销:启动前与收尾后都查 deadline——
            # 最后一次 rerun 之后越过截止时刻,同样不得 resolved(spec S02 必测)
            if (overrun := _time_overrun()) is not None:
                _deadline_terminal(overrun)
                raise _DeadlineReached()
            rerun_failed, _ = run_pytest(
                ctx.python_exe,
                ctx.workspace,
                bug.failed_tests,
                report_dir / "verify-failed-rerun.xml",
                env=ctx.env,
                timeout_seconds=ctx.test_timeout_seconds,
                execution_backend=ctx.execution_backend or None,
            )
            rerun_regression, _ = run_pytest(
                ctx.python_exe,
                ctx.workspace,
                bug.regression_tests,
                report_dir / "verify-regression-rerun.xml",
                env=ctx.env,
                timeout_seconds=ctx.test_timeout_seconds,
                execution_backend=ctx.execution_backend or None,
            )
            if (overrun := _time_overrun()) is not None:
                _deadline_terminal(overrun)
                raise _DeadlineReached()
            mismatch = double_run_mismatch(
                verify_failed, verify_regression, rerun_failed, rerun_regression
            )
            tracker.record(
                tool="verify_double_run",
                state="VERIFY",
                input_payload={"engine": "plain"},
                output_summary={
                    "rerun": {
                        "failed_ok": rerun_failed.all_passed,
                        "regression_ok": rerun_regression.all_passed,
                    },
                    "mismatch": mismatch or None,
                },
                error=f"verify_mismatch: {mismatch}" if mismatch else None,
            )

        diff = working_tree_diff(ctx.workspace)
        result.changed_files = diff.changed_files
        gate = run_gates(
            diff.diff_text,
            allowed_paths=bug.allowed_paths,
            max_files=int(_policy_value("max_patch_files")),
        )
        result.gate_violations = [str(v) for v in gate.violations]
        # 终局共享验收(S02/F1):与 graph 的 finish 节点同一条代码路径
        decision = final_acceptance(
            verify_failed_ok=result.verify_failed_ok,
            verify_regression_ok=result.verify_regression_ok,
            verify_ran=True,
            gate_ok=gate.ok,
            gate_ran=True,
            diff_non_empty=not diff.is_empty,
            double_run_inconsistent=bool(mismatch),
            cancelled=bool(cancel_event is not None and cancel_event.is_set()),
            ledger=ledger,
            deadline_epoch=deadline_epoch,
        )
        result.validation_status = decision.validation_status
        result.gate_status = decision.gate_status
        result.resource_status = decision.resource_status

        # 判定规则(企划书 4.3 + ADR-0009 §1):resolved 与资源/取消绑定
        if mismatch:
            result.status, result.verdict = "NEEDS_REVIEW", "needs_review"
            result.error = f"verify double-run mismatch: {mismatch}"
        elif decision.resolved:
            result.status, result.verdict = "FINISHED", "resolved"
        elif not gate.ok:
            result.status, result.verdict = "PATCH_REJECTED", "failed"
        elif decision.resource_status != "within_budget":
            # 验证过了也不得静默 resolved(ADR-0009 §1):预算语义保持 BUDGET_EXCEEDED
            result.status, result.verdict = "BUDGET_EXCEEDED", "failed"
            result.error = "; ".join(decision.reasons) or result.error
        else:
            # 模型放弃/自认失败/静默结束/取消,结局一致:VERIFY_FAILED(P3 死分支折叠)
            result.status, result.verdict = "VERIFY_FAILED", "failed"

    except BudgetError as exc:
        # N-11 整改:plain 引擎同样把循环已耗的 token/turns 记回账本
        result.status, result.verdict, result.error = "BUDGET_EXCEEDED", "failed", str(exc)
        result.turns = exc.turns
        result.tokens_used = exc.tokens_spent
        result.tokens_prompt = exc.tokens_prompt
        result.tokens_completion = exc.tokens_completion
        result.resource_status = ledger.resource_status
    except TaskCancelled as exc:
        # 协作式取消:保留现场落盘;DB 状态由 cancel_task 置 CANCELLED,回写时让位
        result.status, result.verdict, result.error = "CANCELLED", "cancelled", str(exc)
        result.resource_status = ledger.resource_status
    except _DeadlineReached:
        pass  # 终态已在 _deadline_terminal 里写好(BUDGET_EXCEEDED + exhausted)
    except TaskError as exc:
        result.status, result.verdict, result.error = "INVALID_TASK", "failed", str(exc)
    except Exception as exc:
        log.exception("task %s crashed", task_id)
        result.status, result.verdict, result.error = (
            "NEEDS_REVIEW",
            "needs_review",
            f"{type(exc).__name__}: {exc}",
        )
        result.resource_status = ledger.resource_status
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
        # G03:账本摘要是资源维度的权威读数(与 cost_usd 那条历史 float 约值并存,不互相替代)
        from app.graph.resources import report_summary

        result.resources = report_summary(ledger)
        _write_report(result, run_dir)
        log.info(
            "task %s -> %s/%s (%sms)", task_id, result.status, result.verdict, result.duration_ms
        )

    return result


def run_batch(
    bugs: list[Any],
    model_for: Any,  # Callable[[Any], Model]
    *,
    runs_root: Path,
    max_turns: int = 20,
    engine: str = "plain",
    model_name: str = "",
    blind: bool = False,
) -> list[TaskResult]:
    """批次驱动(E2):逐题执行,收尾写 `runs/<batch>/batch_manifest.json`。

    model_for(bug) 返回该题的模型实例——fake 回放按题装载脚本,真实模型
    通常恒返回同一 client。fail-fast 守卫在批次发起前执行;manifest 的
    started_at/ended_at/config_snapshot 与任务级 provenance 同口径,批次级
    溯源从此不依赖人工记台账。

    blind=True(E6):发起前把每题 issue 替换为固定占位——盲跑对照只给失败
    测试,度量 localize 的真实贡献;测试集原样保留,graph 层零感知。
    """
    settings = get_settings()
    # 豁免同口径:run_task/run_task_graph 都对 fake-replay 豁免(零花费回放传空
    # model_name),批次级守卫此前漏了豁免——本地 .env 开 llm_enabled 时,一跑
    # fake 批(含 CLI)就炸断。provider 由首个模型实例判定,与任务级守卫同源。
    models = [model_for(bug) for bug in bugs]
    provider = getattr(models[0], "provider", "") if models else ""
    require_model_name(model_name, settings.llm_enabled and provider != "fake-replay")
    if blind:
        for bug in bugs:
            apply_blind(bug)
    batch_dir = Path(runs_root).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat(timespec="milliseconds")

    results = [
        run_task(
            bug,
            models[index],
            runs_root=batch_dir,
            max_turns=max_turns,
            engine=engine,
            model_name=model_name,
        )
        for index, bug in enumerate(bugs)
    ]

    verdict_counts: dict[str, int] = {}
    for res in results:
        verdict_counts[res.verdict] = verdict_counts.get(res.verdict, 0) + 1
    commit = git_commit()
    # P3-2:manifest 落盘前反查输入存在性(每批 1 次 subprocess)——fake36 批的
    # 教训是"锚点 commit 上不存在所跑的题",反查不通过必须在 manifest 里留痕
    # 并告警,而不是让报告读者把坏锚当复现口径
    missing = missing_inputs_at_commit(commit, [res.bug_id for res in results])
    if missing:
        log.warning(
            "batch %s: %d input(s) missing at anchor commit %s: %s"
            " — 该批不可按 git_commit 复现,请检查工作树状态",
            batch_dir.name,
            len(missing),
            commit[:12],
            missing,
        )
    manifest = {
        "git_commit": commit,
        "started_at": started_at,
        "ended_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "config_snapshot": config_snapshot(),
        "model": model_name,
        "bug_ids": [res.bug_id for res in results],
        "verdict_counts": verdict_counts,
        "blind": blind,
        # P3-2:工作树状态与输入锚点反查——None=无法判定/未检查,与 false/空 区分
        "worktree_dirty": worktree_dirty(),
        "dirty_fingerprint": dirty_fingerprint(),
        "input_anchor_checked": missing is not None,
        "input_anchor_missing": missing or [],
    }
    (batch_dir / "batch_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    log.info("batch %s done: %s tasks, verdicts=%s", batch_dir.name, len(results), verdict_counts)
    return results


def apply_blind(bug: Any) -> None:
    """盲跑装载(E6):issue 置固定占位;failed/regression 测试集原样保留——
    模型仍可从基线失败输出获得信息,被度量的是"无描述时能否定位"。
    只改 evals 层数据装载,`app/graph/` 零改动。"""
    bug.issue_text = BLIND_ISSUE


BLIND_ISSUE = "Blind run: no issue description provided."


def main(argv: list[str] | None = None) -> int:
    """批次 CLI(E6):`python -m app.evals.driver --bugs all --out runs/xxx [--blind]`。

    fake 模式按题装载 replay 脚本;盲跑批次建议批次名带 -blind 后缀,
    报告展示层读 batch_manifest.json 的 blind 标记。
    """
    parser = argparse.ArgumentParser(description="PatchPilot 批次驱动器")
    parser.add_argument("--bugs", default="all", help='"all" 或逗号分隔的 BUG-xxx 列表')
    parser.add_argument("--model", default="fake", choices=["fake", "openai"])
    # P3-16:本 CLI 只支持 plain——graph 回放脚本喂 plain 循环是假 engine
    # (R3-Q6);graph 引擎走 python -m app.evals.run_single --engine graph
    parser.add_argument("--engine", default="plain", choices=["plain"])
    parser.add_argument("--out", required=True, help="批次 runs 目录")
    parser.add_argument("--blind", action="store_true", help="盲跑对照:issue 置占位")
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.evals.bugset import list_bug_ids, load_bug, load_replay_script
    from app.llm.fake import FakeLLM
    from app.llm.openai_client import build_model

    settings = get_settings()
    if args.bugs.strip().lower() == "all":
        ids = list_bug_ids(Path("bugs"))
    else:
        ids = [b.strip() for b in args.bugs.split(",") if b.strip()]
    bugs = [load_bug(bug_id, Path("bugs")) for bug_id in ids]
    if args.model == "fake":
        scripts = {bug.id: load_replay_script(bug, kind=args.engine) for bug in bugs}

        def model_for(bug: Any) -> Any:
            return FakeLLM(scripts[bug.id])

    else:

        def model_for(bug: Any) -> Any:
            return build_model("openai", settings)

    model_name = settings.llm_model if args.model == "openai" else ""
    results = run_batch(
        bugs,
        model_for,
        runs_root=Path(args.out),
        max_turns=args.max_turns,
        engine=args.engine,
        model_name=model_name,
        blind=args.blind,
    )
    from collections import Counter

    verdicts = dict(Counter(r.verdict for r in results))
    print(f"[batch] {args.out}: {len(results)} tasks, verdicts={verdicts}, blind={args.blind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
