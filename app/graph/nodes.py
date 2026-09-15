"""LangGraph 节点实现:每个节点对应企划书 4.1 的一个状态。

节点闭包持有 ToolContext / 模型等运行时对象;state 里只放可序列化数据。
判定规则(4.3)在 verify 路由与 apply 门禁中落地。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import run_pytest
from app.config import get_settings
from app.errors import TaskError
from app.gitops.differ import working_tree_diff
from app.gitops.testing import materialize_repo
from app.graph.gates import ensure_budget, run_gates
from app.graph.plain_loop import run_plain_loop
from app.graph.state import TaskState
from app.llm.base import Model
from app.prompts import LOCALIZE_PROMPT, PROPOSE_PROMPT, build_feedback
from app.tools.base import ToolContext
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
    started_monotonic: float = 0.0
    ctx: ToolContext | None = None
    baseline_commit: str = ""

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
            )
        except Exception as exc:  # noqa: BLE001
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
            }
        return {
            "status": "NEEDS_REVIEW",
            "outcome": "needs_review",
            "error": f"localize failed: {outcome.summary or 'no finish'}",
            "turns": state.get("turns", 0) + outcome.turns,
        }

    def route_localize(self, state: TaskState) -> str:
        return "end" if state["status"] == "NEEDS_REVIEW" else "continue"

    # ---------- PROPOSE_PATCH ----------

    def propose(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        try:
            ensure_budget(
                round_no=state["round_no"],
                max_rounds=self.max_rounds,
                tokens_used=state.get("tokens_used", 0),
                token_budget=0,
                started_monotonic=self.started_monotonic,
                time_budget_seconds=get_settings().task_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 预算超限是正常业务分支
            return {"status": "BUDGET_EXCEEDED", "outcome": "failed", "error": str(exc)}

        prompt = PROPOSE_PROMPT.format(
            round_no=state["round_no"], feedback=state.get("feedback", "")
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
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "NEEDS_REVIEW", "outcome": "needs_review", "error": f"propose: {exc}"}

        update: dict[str, Any] = {
            "turns": state.get("turns", 0) + outcome.turns,
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
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
        """模型放弃且无补丁 → VERIFY_FAILED 终态;否则进入门禁。"""
        return "end" if state["status"] == "VERIFY_FAILED" else "apply"

    # ---------- APPLY_PATCH ----------

    def apply(self, state: TaskState) -> dict[str, Any]:
        """最终门禁:对整个工作区 diff 复核六项门禁(工具层已逐补丁拦截过)。"""
        assert self.ctx is not None
        diff = working_tree_diff(self.workspace)
        gate = run_gates(
            diff.diff_text,
            allowed_paths=self.bug.allowed_paths,
            max_files=get_settings().max_patch_files,
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
        update: dict[str, Any] = {
            "status": "PATCH_REJECTED",
            "gate_violations": violations,
            "feedback": build_feedback([], "上一轮补丁被门禁拒绝:" + "; ".join(violations)),
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
        if not failed_report.all_passed:
            update["feedback"] = build_feedback(
                [
                    {"name": c.test_name, "signature": c.signature}
                    for c in failed_report.failed_cases
                ]
            )
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
        ok = state["verify_failed_ok"] and state["verify_regression_ok"]
        return "finish" if ok else "rollback"

    def finish(self, state: TaskState) -> dict[str, Any]:
        """判定规则 4.3 的四个条件已由前面的节点保证:门禁(apply)、双测试集(verify)、预算(guard)。"""
        return {"status": "FINISHED", "outcome": "resolved"}

    # ---------- 回滚与预算 ----------

    def rollback(self, state: TaskState) -> dict[str, Any]:
        from app.gitops.rollback import reset_workspace

        reset_workspace(self.workspace, self.baseline_commit)
        self.tracker.record(
            tool="reset_workspace",
            state="PROPOSE_PATCH",
            input_payload={"round": state["round_no"]},
            output_summary={"rolled_back": True},
        )
        if state["round_no"] >= self.max_rounds:
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": "rounds exhausted after failed verify",
            }
        return {
            "status": "PROPOSE_PATCH",
            "round_no": state["round_no"] + 1,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
        }

    def route_rollback(self, state: TaskState) -> str:
        return "end" if state["status"] == "BUDGET_EXCEEDED" else "propose"

    # ---------- 兜底 ----------

    @staticmethod
    def state_error_guard(state: TaskState) -> dict[str, Any]:
        return dict(state)


def build_report_extra(state: TaskState) -> dict[str, Any]:
    return {
        "round_no": state.get("round_no", 1),
        "status": state.get("status"),
        "outcome": state.get("outcome"),
    }


def monotonic_now() -> float:
    return time.monotonic()
