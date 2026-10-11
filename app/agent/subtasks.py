"""G13:子任务编排——ChildResult、SubtaskManager 与统一 child runner(SPEC §G13)。

设计要点:
- 子任务有独立模型会话、精简任务/证据包,不复制父消息、不共享可变 messages;
- 三角色工具集来自 app/agent/roles.py,均不含 delegate/ask_user → 递归深度恒为 1;
- 父账本先原子预留子额度(resources.reserve_subtask),全部子任务合计不超过
  任务有效限额 × share;余量不足拒绝委派,不因分支创建重授墙钟/调用数;
- 返回有界结构化 ChildResult;父 Agent 只吸收摘要/引用——子任务"测试通过"
  不是最终成功授权(候选合流仍走原 apply/gates/verify);
- 一个子任务一个独立模型会话(新 ctx + 新 Tracker),不共享父 messages;
- `run_subtask_loop` 是统一 child runner:delegate 工具与 `_run_candidate`(分支候选)
  共用同一套循环包装,分支候选同样经 manager 计名额、记 ChildResult。

未配置口径(FakeLLM 子任务):缺 replay_subtasks 脚本时 model_factory 返回 None,
delegate 明确返回 termination="unconfigured",绝不沿用父脚本游标或偷调真实模型。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agent.roles import ROLE_REGISTRY
from app.errors import BudgetError, TaskCancelled
from app.graph.resources import ResourceLedger
from app.llm.base import Model
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

if TYPE_CHECKING:
    from app.evals.bugset import BugTask

log = logging.getLogger(__name__)

#: 子任务终止原因(报告与轨迹判读用,不是模型自述)
TERM_FINISHED = "finished"
TERM_BUDGET = "budget"
TERM_CRASHED = "crashed"
TERM_CANCELLED = "cancelled"
TERM_REFUSED = "refused"
TERM_UNCONFIGURED = "unconfigured"


@dataclass
class ChildResult:
    """有界结构化子任务返回:结论/证据引用/未验证假设/风险/候选引用/计量/终止原因。"""

    role: str
    subtask_id: str
    ok: bool
    conclusion: str
    evidence_ids: list[str] = field(default_factory=list)
    unverified_assumptions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    candidate_id: str | None = None
    candidate_hash: str | None = None
    turns: int = 0
    tokens_used: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    termination: str = TERM_REFUSED
    error: str = ""

    @classmethod
    def refused(cls, role: str, subtask_id: str, reason: str) -> ChildResult:
        return cls(
            role=role,
            subtask_id=subtask_id,
            ok=False,
            conclusion="",
            termination=TERM_REFUSED,
            error=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "subtask_id": self.subtask_id,
            "ok": self.ok,
            "conclusion": self.conclusion,
            "evidence_ids": list(self.evidence_ids),
            "unverified_assumptions": list(self.unverified_assumptions),
            "risks": list(self.risks),
            "candidate_id": self.candidate_id,
            "candidate_hash": self.candidate_hash,
            "turns": self.turns,
            "tokens_used": self.tokens_used,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "termination": self.termination,
            "error": self.error,
        }


def run_subtask_loop(
    ctx: ToolContext,
    model: Model,
    prompt: str,
    *,
    max_turns: int,
    round_no: int,
    state_label: str,
    allowed_tools: list[str],
    token_budget: int | None,
    deadline_epoch: float | None,
    cancel_event: threading.Event | None,
    runtime: Any,
    ledger: ResourceLedger,
    absolute_deadline_epoch: float | None = None,
) -> Any:
    """统一 child runner:与主循环同一套 run_plain_loop,但会话/轮次/预算都独立。

    `runtime.loop_settings()` 由父节点冻结的 TaskRuntime 提供(子任务与父同一套
    运行 profile);allowed_tools 由角色表决定,执行前同 schema 装配一起挡人。
    """
    from app.graph.plain_loop import run_plain_loop

    return run_plain_loop(
        ctx,
        model,
        prompt,
        max_turns=max_turns,
        round_no=round_no,
        state_label=state_label,
        extra_system="",
        allowed_tools=allowed_tools,
        token_budget=token_budget,
        deadline_epoch=deadline_epoch,
        absolute_deadline_epoch=absolute_deadline_epoch,
        cancel_event=cancel_event,
        **runtime.loop_settings(),
        ledger=ledger,
    )


def _subtask_ctx(
    parent_ctx: ToolContext,
    *,
    task_id: str,
    workspace: Path,
    baseline_commit: str,
    report_dir: Path,
) -> ToolContext:
    """子任务的独立会话上下文:继承父任务的执行参数与限制,不共享 messages/记忆。

    刻意不带 session/memory/evidence:子任务是精简的独立调查,不把观测写进父任务
    记忆、不共享崩溃恢复会话;只读角色的 workspace 复用父工作区,但绝不改动它。
    """
    return ToolContext(
        task_id=task_id,
        workspace=workspace,
        baseline_commit=baseline_commit,
        tracker=Tracker(report_dir / "trajectory.jsonl", task_id=task_id),
        report_dir=report_dir,
        env=parent_ctx.env,
        python_exe=parent_ctx.python_exe,
        test_sets=parent_ctx.test_sets,
        allowed_paths=parent_ctx.allowed_paths,
        whitelist=parent_ctx.whitelist,
        max_read_lines=parent_ctx.max_read_lines,
        max_search_results=parent_ctx.max_search_results,
        max_patch_files=parent_ctx.max_patch_files,
        test_timeout_seconds=parent_ctx.test_timeout_seconds,
        forbid_test_files=parent_ctx.forbid_test_files,
        execution_backend=parent_ctx.execution_backend,
        cancel_event=parent_ctx.cancel_event,
        mode=parent_ctx.mode,
    )


@dataclass
class SubtaskManager:
    """子任务名额、额度与执行的唯一编排点(一个任务一个实例)。"""

    #: 委派总开关(冻结自 Settings.delegation_enabled)
    enabled: bool
    #: 最多同时运行的子执行者数(并发上限)
    max_concurrency: int
    #: 每任务最多委派次数(含 Best-of-N 候选各消耗的一个名额)
    max_delegations: int
    #: 单个子任务轮次上限
    max_turns: int
    #: 角色 → 子任务模型;None = 未配置(明确拒绝委派)
    model_factory: Callable[[str], Model] | None
    #: 父任务级账本(预留/结算/用量都在这一份上)
    ledger: ResourceLedger
    #: 子任务报告目录的父级(与候选同层,`report_dir/sub<序号>` 落轨迹)
    report_dir: Path
    #: 题目(repair_candidate 要物化自己的工作区;只读角色用不到 repo_dir)
    bug: BugTask | None = None
    #: 角色 + 任务描述 + 证据引用 + 候选引用 → 子任务系统提示
    prompt_builder: Callable[[str, str, list[str], str | None], str] | None = None
    #: 父运行 profile(子任务与父同一套 loop_settings)
    runtime: Any = None
    #: 父任务墙钟截止(子任务共享同一 deadline,不重授)
    deadline_epoch: float | None = None
    absolute_deadline_epoch: float | None = None
    cancel_event: threading.Event | None = None
    #: 已记录的子任务结果(报告与名额对账的唯一事实源)
    children: list[ChildResult] = field(default_factory=list)
    _active: int = field(default=0, init=False)
    _seq: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ---------- 名额 ----------

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"sub{self._seq}"

    def slots_remaining(self) -> int:
        with self._lock:
            return max(self.max_delegations - len(self.children), 0)

    def acquire_slot(self) -> bool:
        """占一个执行名额(次数与并发一起检查;失败返回 False,不抛)。"""
        with self._lock:
            if not self.enabled:
                return False
            if len(self.children) >= self.max_delegations:
                return False
            if self._active >= self.max_concurrency:
                return False
            self._active += 1
            return True

    def release_slot(self) -> None:
        with self._lock:
            self._active = max(self._active - 1, 0)

    def record_child(self, child: ChildResult) -> None:
        with self._lock:
            self.children.append(child)

    # ---------- 执行 ----------

    def delegate(
        self,
        *,
        role: str,
        task: str,
        evidence_ids: list[str],
        candidate_id: str | None,
        ctx: ToolContext,
    ) -> ChildResult:
        """delegate 工具的入口:校验角色 → 占名额 → 预留额度 → 跑子任务 → 结算。"""
        spec = ROLE_REGISTRY.get(role)
        subtask_id = self._next_id()
        if spec is None:
            return ChildResult.refused(role, subtask_id, f"unknown role: {role}")
        if not self.acquire_slot():
            return ChildResult.refused(role, subtask_id, self._denial_reason(role))
        try:
            model = None
            if self.model_factory is None:
                return ChildResult.refused(
                    role, subtask_id, "subtask model unconfigured (delegation disabled)"
                )
            model = self.model_factory(role)
            if model is None:
                return ChildResult.refused(
                    role, subtask_id, f"subtask model unconfigured for role {role}"
                )
            reserved = self._per_subtask_budget()
            if reserved and not self.ledger.reserve_subtask(tokens=reserved, stage=role):
                return ChildResult.refused(role, subtask_id, "subtask budget insufficient")
            if self.prompt_builder is None:
                prompt = self._default_prompt(role, task, evidence_ids, candidate_id)
            else:
                prompt = self.prompt_builder(role, task, evidence_ids, candidate_id)
            return self._run_subtask(
                spec=spec,
                task=task,
                evidence_ids=evidence_ids,
                candidate_id=candidate_id,
                ctx=ctx,
                model=model,
                prompt=prompt,
                subtask_id=subtask_id,
                token_budget=reserved,
            )
        finally:
            self.release_slot()

    def _denial_reason(self, role: str) -> str:
        with self._lock:
            if not self.enabled:
                return "delegation disabled"
            if len(self.children) >= self.max_delegations:
                return f"delegation slots exhausted (max_delegations={self.max_delegations})"
            return (
                f"subagent concurrency limit reached (max_subagent_concurrency="
                f"{self.max_concurrency})"
            )

    def _per_subtask_budget(self) -> int:
        """单子任务预留 = 子任务池上限均分给名额;无任务级限额 = 0(运行时不限)。"""
        ceiling = self.ledger.subtask_ceiling()
        if ceiling is None:
            return 0
        return max(ceiling // self.max_delegations, 1)

    def _default_prompt(
        self, role: str, task: str, evidence_ids: list[str], candidate_id: str | None
    ) -> str:
        spec = ROLE_REGISTRY[role]
        evidence = "\n".join(f"- {eid}" for eid in evidence_ids) or "(无)"
        candidate = candidate_id or "(无)"
        return (
            f"你是任务的{role}子执行者。\n{spec.description}\n\n"
            f"## 委派任务\n{task}\n\n"
            f"## 证据引用\n{evidence}\n\n"
            f"## 候选引用\n{candidate}\n\n"
            "完成后用 finish 返回结论;只陈述你实际观测/验证到的事实,"
            "未验证的假设与风险要分开列出。"
        )

    def _run_subtask(
        self,
        *,
        spec: Any,
        task: str,
        evidence_ids: list[str],
        candidate_id: str | None,
        ctx: ToolContext,
        model: Model,
        prompt: str,
        subtask_id: str,
        token_budget: int,
    ) -> ChildResult:
        workspace = ctx.workspace
        baseline = ctx.baseline_commit
        report_dir = self.report_dir / subtask_id
        report_dir.mkdir(parents=True, exist_ok=True)
        if spec.isolated_workspace:
            if self.bug is None:
                return ChildResult.refused(
                    spec.name, subtask_id, "repair_candidate requires bug context"
                )
            from app.gitops.testing import materialize_repo

            workspace = self.report_dir / f"workspace-{subtask_id}"
            baseline = materialize_repo(self.bug.repo_dir, workspace, extra_commit=False)
        sub_ctx = _subtask_ctx(
            ctx,
            task_id=f"{ctx.task_id}-{subtask_id}",
            workspace=workspace,
            baseline_commit=baseline,
            report_dir=report_dir,
        )
        try:
            outcome = run_subtask_loop(
                sub_ctx,
                model,
                prompt,
                max_turns=self.max_turns,
                round_no=1,
                state_label=f"SUB-{spec.name.upper()}",
                allowed_tools=list(spec.tools),
                token_budget=token_budget or None,
                deadline_epoch=self.deadline_epoch,
                absolute_deadline_epoch=self.absolute_deadline_epoch,
                cancel_event=self.cancel_event,
                runtime=self.runtime,
                ledger=self.ledger,
            )
            termination = TERM_FINISHED
            error = ""
            if outcome.completion == "clarification_required":
                error = f"clarification_required: {outcome.protocol_reason}"
            result = ChildResult(
                role=spec.name,
                subtask_id=subtask_id,
                ok=outcome.success,
                conclusion=outcome.summary,
                turns=outcome.turns,
                tokens_used=outcome.tokens_used,
                tokens_prompt=outcome.tokens_prompt,
                tokens_completion=outcome.tokens_completion,
                termination=termination,
                error=error,
            )
        except TaskCancelled as exc:
            result = ChildResult(
                role=spec.name,
                subtask_id=subtask_id,
                ok=False,
                conclusion="",
                termination=TERM_CANCELLED,
                error=str(exc),
            )
        except BudgetError as exc:
            # 子任务超额:真实用量照记(BudgetError 携带),不抹掉失败子任务的花费
            result = ChildResult(
                role=spec.name,
                subtask_id=subtask_id,
                ok=False,
                conclusion=f"预算超限:{exc}",
                turns=getattr(exc, "turns", 0),
                tokens_used=getattr(exc, "tokens_spent", 0),
                tokens_prompt=getattr(exc, "tokens_prompt", 0),
                tokens_completion=getattr(exc, "tokens_completion", 0),
                termination=TERM_BUDGET,
                error=str(exc),
            )
        except Exception as exc:  # 子任务崩溃不拖垮父任务:按未完成收敛
            log.exception("subtask %s crashed", subtask_id)
            result = ChildResult(
                role=spec.name,
                subtask_id=subtask_id,
                ok=False,
                conclusion="",
                termination=TERM_CRASHED,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if token_budget:
                self.ledger.settle_subtask(
                    reserved_tokens=token_budget, tokens_used=result.tokens_used
                )
        # 隔离工作区的产物作为候选引用:记录 diff hash(只读角色不产生候选)
        if spec.isolated_workspace and not result.error:
            try:
                from app.gitops.differ import working_tree_diff

                diff_text = working_tree_diff(workspace).diff_text
                if diff_text.strip():
                    result.candidate_id = subtask_id
                    result.candidate_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
            except Exception:
                log.exception("subtask %s: failed to read candidate diff", subtask_id)
        self.record_child(result)
        return result
