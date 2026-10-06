"""LangGraph 节点实现:每个节点对应企划书 4.1 的一个状态。

节点闭包持有 ToolContext / 模型等运行时对象;state 里只放可序列化数据。
判定规则(4.3)在 verify 路由与 apply 门禁中落地。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import run_pytest
from app.config import get_settings
from app.errors import BudgetError, TaskCancelled, TaskError
from app.gitops.differ import working_tree_diff
from app.gitops.testing import materialize_repo
from app.graph.gates import ensure_budget, run_gates
from app.graph.plain_loop import run_plain_loop
from app.graph.state import TaskState
from app.llm.base import Model
from app.prompts import LOCALIZE_PROMPT, PROPOSE_PROMPT, build_feedback
from app.tools.base import ToolContext
from app.tools.output_filter import refine_traceback
from app.tools.registry import FINISH_TOOL
from app.tools.tracker import Tracker

log = logging.getLogger(__name__)

READ_TOOLS = ["list_files", "search_code", "read_file", "git_diff", FINISH_TOOL]
WRITE_TOOLS = [
    "list_files",
    "search_code",
    "read_file",
    "git_diff",
    "apply_patch",
    "run_tests",
    FINISH_TOOL,
]


def _case_ids(report: Any) -> set[tuple[str, str, str]]:
    """junit 两次运行比对用的测试 id 集合(file, class, name 三元组,不含判定)。"""
    return {(file_attr, classname, name) for file_attr, classname, name, _ in report.case_results}


def _double_run_mismatch(
    first_failed: Any, first_reg: Any, rerun_failed: Any, rerun_reg: Any
) -> str:
    """比对 verify 双跑的两次 junit;一致返回空串,不一致返回不匹配原因。

    一致 = 两个测试集各自满足:两次收集到的测试 id 集合相等,且第一遍
    (全绿)通过的 id 在第二遍无任何非 passed 记录(rerun 亦须 all_passed)。
    """
    for label, first, rerun in (
        ("failed", first_failed, rerun_failed),
        ("regression", first_reg, rerun_reg),
    ):
        if _case_ids(first) != _case_ids(rerun):
            return f"{label}: junit test id set differs between runs"
        if not rerun.all_passed:
            return f"{label}: rerun not all passed (first run was)"
    return ""


def _feedback_streak(state: TaskState, current: list[str]) -> tuple[list[str], int]:
    """归一化本轮反馈特征串,并计算"连续相同"轮数。

    签名排序后全量比对:完全一致才累加,任何变化都从 1 重新计数;空白
    输入(全绿)归零。verify 失败与门禁拒绝共用同一对状态字段——两类
    特征串永不会相等,不会互相误认成"同一个错误"。
    """
    normalized = sorted(current)
    if not normalized:
        return [], 0
    previous = sorted(state.get("last_feedback_signatures", []))
    streak = state.get("repeat_streak", 0) + 1 if normalized == previous else 1
    return normalized, streak


@dataclass
class TaskNodes:
    """一个任务一次图执行的节点集合(闭包状态,不进 LangGraph state)。"""

    bug: Any  # BugTask
    model: Model
    workspace: Path
    tracker: Tracker
    report_dir: Path
    max_rounds: int
    max_turns: int
    # R2 整改:默认 None(不查时间)而不是 0.0——0.0 会让直接构造 TaskNodes 的
    # 调用方(测试/未来代码)在第一个 turn 边界就撞上"已超时 900s"的假 BudgetError
    started_monotonic: float | None = None
    ctx: ToolContext | None = None
    baseline_commit: str = ""
    cancel_event: threading.Event | None = None
    # 卡5 自适应分支:第 i 个候选用哪个模型。None = 不分支(单线,与 V1 同行为)。
    # 带默认值,runner/service 的关键字构造不受影响;由调用方注入,graph 层不 import 测试替身。
    branch_model_factory: Callable[[int], Model] | None = None

    # ---------- CREATED ----------

    def prepare(self, state: TaskState) -> dict[str, Any]:
        """物化题目仓库为 git 工作区,固定基线 commit。"""
        try:
            self.baseline_commit = materialize_repo(
                self.bug.repo_dir, self.workspace, extra_commit=False
            )
            settings = get_settings()
            self.ctx = ToolContext(
                task_id=state["bug_id"],
                workspace=self.workspace,
                baseline_commit=self.baseline_commit,
                tracker=self.tracker,
                report_dir=self.report_dir,
                test_sets=self.bug.test_sets,
                allowed_paths=self.bug.allowed_paths,
                max_patch_files=settings.max_patch_files,
                test_timeout_seconds=settings.test_timeout_seconds,
                # P1-4 整改:资源上限此前只落在硬编码默认值上,settings 改了不生效
                max_read_lines=settings.max_read_lines,
                max_search_results=settings.max_search_results,
            )
            self.tracker.record(
                tool="create_workspace",
                state="BASELINE",
                input_payload={"baseline": self.baseline_commit[:12]},
            )
            return {"status": "BASELINE", "baseline_regression_ok": False}
        except TaskError as exc:
            return {"status": "INVALID_TASK", "error": str(exc), "outcome": "invalid"}

    def route_prepare(self, state: TaskState) -> str:
        return "end" if state["status"] == "INVALID_TASK" else "continue"

    # ---------- BASELINE ----------

    def baseline(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        failed_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.failed_tests,
            self.report_dir / "baseline-failed.xml",
        )
        regression_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.regression_tests,
            self.report_dir / "baseline-regression.xml",
        )
        signature = (
            failed_report.failed_cases[0].signature if failed_report.failed_cases else "(none)"
        )
        update: dict[str, Any] = {
            "baseline_failed": failed_report.failed + failed_report.errors,
            "baseline_regression_ok": regression_report.all_passed,
            "failure_signature": signature,
        }
        if failed_report.all_passed:
            update.update(
                status="INVALID_TASK",
                error="baseline: failed_tests already pass",
                outcome="invalid",
            )
        elif not regression_report.all_passed:
            update.update(
                status="INVALID_TASK", error="baseline: regression set not green", outcome="invalid"
            )
        else:
            update.update(status="LOCALIZE")
            self.tracker.record(
                tool="baseline", state="LOCALIZE", input_payload={"signature": signature}
            )
        return update

    def route_baseline(self, state: TaskState) -> str:
        return "end" if state["status"] == "INVALID_TASK" else "continue"

    # ---------- LOCALIZE ----------

    def localize(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        try:
            outcome = run_plain_loop(
                self.ctx,
                self.model,
                LOCALIZE_PROMPT.format(
                    issue_text=self.bug.issue_text,
                    failed_tests="\n".join(f"- {t}" for t in self.bug.failed_tests),
                ),
                max_turns=self.max_turns,
                round_no=state["round_no"],
                state_label="LOCALIZE",
                allowed_tools=READ_TOOLS,
                started_monotonic=self.started_monotonic,
                time_budget_seconds=get_settings().task_timeout_seconds,
                token_budget=self._token_budget_for(state),
                cancel_event=self.cancel_event,
            )
        except BudgetError as exc:
            # N-5 整改:BUDGET_EXCEEDED 必须是转移终点(route_localize 会 end),
            # 否则超预算后 propose 照跑、资源门禁被"顺路"绕过。
            # N-11 整改:把循环已烧掉的 token/turns 记回任务级账本,不再蒸发
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": f"localize: {exc}",
                "turns": state.get("turns", 0) + getattr(exc, "turns", 0),
                "tokens_used": state.get("tokens_used", 0) + getattr(exc, "tokens_spent", 0),
                "tokens_prompt": state.get("tokens_prompt", 0) + getattr(exc, "tokens_prompt", 0),
                "tokens_completion": state.get("tokens_completion", 0)
                + getattr(exc, "tokens_completion", 0),
            }
        except TaskCancelled:
            raise  # 交给 runner 收敛为 CANCELLED,不得吞成 NEEDS_REVIEW
        except Exception as exc:
            return {
                "status": "NEEDS_REVIEW",
                "outcome": "needs_review",
                "error": f"localize: {exc}",
            }

        if outcome.finish_declared and outcome.success:
            return {
                "status": "PROPOSE_PATCH",
                "findings": outcome.summary,
                "turns": state.get("turns", 0) + outcome.turns,
                "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
                "tokens_prompt": state.get("tokens_prompt", 0) + outcome.tokens_prompt,
                "tokens_completion": state.get("tokens_completion", 0) + outcome.tokens_completion,
            }
        return {
            "status": "NEEDS_REVIEW",
            "outcome": "needs_review",
            "error": f"localize failed: {outcome.summary or 'no finish'}",
            "turns": state.get("turns", 0) + outcome.turns,
        }

    def route_localize(self, state: TaskState) -> str:
        """定位失败/超预算都是终点(N-5):超预算后不得继续 propose。"""
        return "end" if state["status"] in ("NEEDS_REVIEW", "BUDGET_EXCEEDED") else "continue"

    def _token_budget_for(self, state: TaskState) -> int | None:
        """本循环可用的 token 余量(N-11 整改)。

        此前每个循环都各自拿满 Settings.token_budget——localize 烧满后 propose
        又是全新一份,任务级真实消耗可达配置的数倍。None 表示任务级不限制。
        调用方需先处理余量已耗尽的情形(0 会被 run_plain_loop 当作"不限制")。
        """
        settings = get_settings()
        if settings.token_budget <= 0:
            return None
        return max(settings.token_budget - state.get("tokens_used", 0), 1)

    # ---------- PROPOSE_PATCH ----------

    def propose(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        # started_monotonic 未接线时(直接构造 TaskNodes 的测试场景)以"当前"为
        # 时间零点,时间预算从 propose 起算;生产路径由 runner 赋任务真实起点
        started = self.started_monotonic if self.started_monotonic is not None else time.monotonic()
        try:
            ensure_budget(
                round_no=state["round_no"],
                max_rounds=self.max_rounds,
                tokens_used=state.get("tokens_used", 0),
                token_budget=get_settings().token_budget,
                started_monotonic=started,
                time_budget_seconds=get_settings().task_timeout_seconds,
            )
        except BudgetError as exc:
            # 只捕 BudgetError:其余异常是实现缺陷,交给 runner 收敛为 NEEDS_REVIEW,
            # 不得伪装成"预算超限"污染终态语义(N-5 同源)
            return {"status": "BUDGET_EXCEEDED", "outcome": "failed", "error": str(exc)}

        prompt = PROPOSE_PROMPT.format(
            round_no=state["round_no"],
            issue_text=state["issue_text"],
            # P1-1 整改:PROPOSE 是全新会话,必须带全 Bug 描述与定位结论,
            # 否则真实模型在 PROPOSE 阶段"盲改"(FakeLLM 回放照不出)
            findings=state.get("findings") or "(定位阶段未给出结论;请先用只读工具确认根因)",
            feedback=state.get("feedback", ""),
        )
        try:
            outcome = run_plain_loop(
                self.ctx,
                self.model,
                prompt,
                max_turns=self.max_turns,
                round_no=state["round_no"],
                state_label="PROPOSE_PATCH",
                allowed_tools=WRITE_TOOLS,
                started_monotonic=started,
                time_budget_seconds=get_settings().task_timeout_seconds,
                token_budget=self._token_budget_for(state),
                cancel_event=self.cancel_event,
            )
        except BudgetError as exc:
            # N-5 整改:超预算必须终止(route_propose 会 end),不得带着已应用
            # 的补丁继续 apply/verify 把资源门禁绕过去。
            # N-11 整改:已耗 token/turns 记回任务级账本
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": f"propose: {exc}",
                "turns": state.get("turns", 0) + getattr(exc, "turns", 0),
                "tokens_used": state.get("tokens_used", 0) + getattr(exc, "tokens_spent", 0),
                "tokens_prompt": state.get("tokens_prompt", 0) + getattr(exc, "tokens_prompt", 0),
                "tokens_completion": state.get("tokens_completion", 0)
                + getattr(exc, "tokens_completion", 0),
            }
        except TaskCancelled:
            raise  # 交给 runner 收敛为 CANCELLED,不得吞成 NEEDS_REVIEW
        except Exception as exc:
            return {"status": "NEEDS_REVIEW", "outcome": "needs_review", "error": f"propose: {exc}"}

        update: dict[str, Any] = {
            "turns": state.get("turns", 0) + outcome.turns,
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
            "tokens_prompt": state.get("tokens_prompt", 0) + outcome.tokens_prompt,
            "tokens_completion": state.get("tokens_completion", 0) + outcome.tokens_completion,
        }
        if outcome.finish_declared and not outcome.success and not outcome.patch_applied:
            # 模型声明放弃且没有任何补丁 → VERIFY_FAILED 终态
            update.update(
                status="VERIFY_FAILED", outcome="failed", error=f"agent gave up: {outcome.summary}"
            )
        else:
            update["status"] = "APPLY_PATCH"
        return update

    def route_propose(self, state: TaskState) -> str:
        """模型放弃且无补丁 → VERIFY_FAILED 终态;超预算 → 终点(N-5);否则进入门禁。"""
        return "end" if state["status"] in ("VERIFY_FAILED", "BUDGET_EXCEEDED") else "apply"

    # ---------- APPLY_PATCH ----------

    def apply(self, state: TaskState) -> dict[str, Any]:
        """最终门禁:对整个工作区 diff 复核七项门禁的静态五项
        (格式/文件/路径/影子/范围;命令与资源门禁分别在工具层与预算检查点拦截)。"""
        assert self.ctx is not None
        diff = working_tree_diff(self.workspace)
        gate = run_gates(
            diff.diff_text,
            allowed_paths=self.bug.allowed_paths,
            max_files=get_settings().max_patch_files,
            forbid_test_files=self.ctx.forbid_test_files,
        )
        if gate.ok and not diff.is_empty:
            self.tracker.record(
                tool="apply_gate", state="VERIFY", input_payload={"files": diff.changed_files}
            )
            return {"status": "VERIFY", "changed_files": diff.changed_files, "gate_violations": []}

        violations = [str(v) for v in gate.violations] or ["[format] no patch applied"]
        self.tracker.record(
            tool="apply_gate",
            state="VERIFY",
            input_payload={},
            output_summary={"violations": violations},
            error=violations[0],
        )
        signatures, streak = _feedback_streak(state, violations)
        update: dict[str, Any] = {
            "status": "PATCH_REJECTED",
            "gate_violations": violations,
            "feedback": build_feedback(
                [], "上一轮补丁被门禁拒绝:" + "; ".join(violations), repeat_streak=streak
            ),
            "last_feedback_signatures": signatures,
            "repeat_streak": streak,
        }
        if state["round_no"] < self.max_rounds:
            # 转移表:PATCH_REJECTED 且轮数未超 → 回 PROPOSE 重试;重试计入轮数
            update["round_no"] = state["round_no"] + 1
        return update

    def route_apply(self, state: TaskState) -> str:
        if state["status"] == "VERIFY":
            return "verify"
        # PATCH_REJECTED:轮数未超 → 回 PROPOSE 重试;超了 → BUDGET_EXCEEDED(企划书 4.2)
        if state["round_no"] < self.max_rounds:
            return "retry"
        return "exhausted"

    # ---------- VERIFY ----------

    def verify(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        failed_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.failed_tests,
            self.report_dir / "verify-failed.xml",
        )
        regression_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.regression_tests,
            self.report_dir / "verify-regression.xml",
        )
        update: dict[str, Any] = {
            "verify_failed_ok": failed_report.all_passed,
            "verify_regression_ok": regression_report.all_passed,
            "status": "VERIFY",
        }
        # 回归集单独失败时也要有反馈(此前只处理 failed 集,回归失败反馈会悬空);
        # failed+regression 的失败签名合并成一组做"连续相同"检测(重复错误 → 换思路提示)
        failing_cases = [
            {
                "name": c.test_name,
                "signature": c.signature,
                "traceback": refine_traceback(c.traceback, self.workspace),
            }
            for c in [*failed_report.failed_cases, *regression_report.failed_cases]
        ]
        if failing_cases:
            signatures, streak = _feedback_streak(
                state, [f"{c['name']}|{c['signature']}" for c in failing_cases]
            )
            update["feedback"] = build_feedback(failing_cases, repeat_streak=streak)
            update["last_feedback_signatures"] = signatures
            update["repeat_streak"] = streak
        # E3 double-run 分支:仅当第一遍双测试集全绿(即即将判 resolved)时,
        # 用相同命令再跑一遍并比对两次 junit——判定面与被测代码同机(threat-model
        # §4 不防 junit 伪造),本复核防的是非自适应偶发伪绿;对基线预置伪造的
        # 自适应对手无实质检出力(同一伪造逻辑两次运行同样生效)。
        # 非 resolved 路径(本就失败)不多跑,开销不变;resolved 判定规则本身不改。
        mismatch = ""
        if (
            failed_report.all_passed
            and regression_report.all_passed
            and get_settings().verify_double_run
        ):
            rerun_failed, _ = run_pytest(
                self.ctx.python_exe,
                self.workspace,
                self.bug.failed_tests,
                self.report_dir / "verify-failed-rerun.xml",
            )
            rerun_regression, _ = run_pytest(
                self.ctx.python_exe,
                self.workspace,
                self.bug.regression_tests,
                self.report_dir / "verify-regression-rerun.xml",
            )
            mismatch = _double_run_mismatch(
                failed_report, regression_report, rerun_failed, rerun_regression
            )
            self.tracker.record(
                tool="verify_double_run",
                state="VERIFY",
                input_payload={"round": state["round_no"]},
                output_summary={
                    "first": {
                        "failed_ok": failed_report.all_passed,
                        "regression_ok": regression_report.all_passed,
                    },
                    "rerun": {
                        "failed_ok": rerun_failed.all_passed,
                        "regression_ok": rerun_regression.all_passed,
                    },
                    "mismatch": mismatch or None,
                },
                error=f"verify_mismatch: {mismatch}" if mismatch else None,
            )
            if mismatch:
                return {
                    "status": "NEEDS_REVIEW",
                    "outcome": "needs_review",
                    "error": f"verify: verify_mismatch ({mismatch})",
                    "verify_failed_ok": False,
                    "verify_regression_ok": False,
                }
        self.tracker.record(
            tool="verify",
            state="VERIFY",
            input_payload={"round": state["round_no"]},
            output_summary={
                "failed_ok": failed_report.all_passed,
                "regression_ok": regression_report.all_passed,
            },
        )
        return update

    def route_verify(self, state: TaskState) -> str:
        if state["status"] == "NEEDS_REVIEW":
            return "end"
        ok = state["verify_failed_ok"] and state["verify_regression_ok"]
        return "finish" if ok else "rollback"

    def finish(self, state: TaskState) -> dict[str, Any]:
        """判定规则 4.3 的四个条件已由前面的节点保证:门禁(apply)、双测试集(verify)、预算(guard)。"""
        return {"status": "FINISHED", "outcome": "resolved"}

    # ---------- 回滚与预算 ----------

    def _should_branch(self, state: TaskState) -> bool:
        """自适应分支触发判定:默认单线,只在"原地打转"证据出现时开一轮候选。

        两类触发信号(任一满足即计):
        ①同一组失败连续 2 轮完全一致(`repeat_streak`,verify 失败与门禁拒绝共用
          该计数但特征串永不互认);
        ②补丁连续 2 次**应用**失败(`ctx.patch_fail_streak`;门禁/协议拒绝是
          "可修正的单线反馈",不计数,否则一次打字错误就会烧双倍预算)。

        其余条件全是"值不值得分支"的前置:开关、本任务尚未分支过、候选模型可用、
        剩余轮数 ≥ 2、token 余量够付一次双倍 propose。任一不满足就继续单线——
        特别是 `branch_model_factory is None`:调用方不注入即整体降级为 V1 行为。
        """
        settings = get_settings()
        if not settings.adaptive_branching_enabled:
            return False
        if state.get("branching_used", False):
            return False
        if self.branch_model_factory is None:
            return False
        if state["round_no"] > self.max_rounds - 2:
            return False  # 分支后至少要留一轮验证
        repeat_streak = state.get("repeat_streak", 0)
        patch_fail_streak = self.ctx.patch_fail_streak if self.ctx else 0
        if repeat_streak < 2 and patch_fail_streak < 2:
            return False
        remaining = self._token_budget_for(state)
        # 预算前置:不靠事后 BudgetError 兜底(remaining is None = 任务级不限制)
        return remaining is None or remaining >= settings.branching_min_token_reserve

    def rollback(self, state: TaskState) -> dict[str, Any]:
        # N-12 整改:回滚会 reset 掉工作区,末轮(验证失败/门禁拒绝)的直接证据
        # 必须先取下——否则 runner 落盘的 diff.patch 是回滚后的空 diff,取证现场被毁
        from app.gitops.differ import working_tree_diff
        from app.gitops.rollback import reset_workspace

        preserved = working_tree_diff(self.workspace).diff_text
        reset_workspace(self.workspace, self.baseline_commit)
        self.tracker.record(
            tool="reset_workspace",
            state="PROPOSE_PATCH",
            input_payload={"round": state["round_no"]},
            output_summary={"rolled_back": True, "preserved_diff_bytes": len(preserved)},
        )
        if state["round_no"] >= self.max_rounds:
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": "rounds exhausted after failed verify",
                "preserved_diff": preserved,
            }
        return {
            "status": "PROPOSE_PATCH",
            "round_no": state["round_no"] + 1,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
        }

    def route_rollback(self, state: TaskState) -> str:
        return "end" if state["status"] == "BUDGET_EXCEEDED" else "propose"
