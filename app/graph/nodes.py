"""LangGraph 节点实现:每个节点对应企划书 4.1 的一个状态。

节点闭包持有 ToolContext / 模型等运行时对象;state 里只放可序列化数据。
判定规则(4.3)在 verify 路由与 apply 门禁中落地。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adapters.pytest_adapter import run_pytest
from app.config import get_settings
from app.context.repo_map import repo_map_for_workspace
from app.errors import BudgetError, TaskCancelled, TaskError
from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch as git_apply_patch

if TYPE_CHECKING:  # 复盘 R-2:兑现 AGENTS"全量类型标注",运行时零导入成本
    from app.evals.bugset import BugTask
from app.gitops.testing import materialize_repo
from app.graph.gates import ensure_budget, run_gates
from app.graph.loop_state import LoopSnapshot
from app.graph.plain_loop import LoopOutcome, run_plain_loop
from app.graph.state import TaskState
from app.llm.base import Model
from app.prompts import (
    LOCALIZE_PROMPT,
    PROPOSE_PROMPT,
    build_feedback,
    build_plan_prompt,
    plan_block_for_propose,
)
from app.tools.base import ToolContext
from app.tools.output_filter import refine_traceback
from app.tools.registry import FINISH_TOOL
from app.tools.tracker import Tracker

log = logging.getLogger(__name__)

READ_TOOLS = [
    "list_files",
    "search_code",
    "find_symbol",
    "describe_file",
    "read_file",
    "git_diff",
    FINISH_TOOL,
]
WRITE_TOOLS = [
    "list_files",
    "search_code",
    "find_symbol",
    "describe_file",
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
class BranchCandidate:
    """一个候选分支的隔离验证结果(择优的唯一依据)。"""

    index: int
    workspace: Path
    diff_text: str
    failed_ok: bool
    regression_ok: bool
    failed_remaining: int
    failed_cases: list[dict[str, str]]
    turns: int
    tokens_used: int
    tokens_prompt: int
    tokens_completion: int
    error: str = ""


VARIANT_HINTS: dict[int, str] = {
    0: (
        "## 本轮要求:换思路(方向 A)\n"
        "上一轮的同一种改法没有修好。请重新核对根因与前置条件:"
        "先确认失败断言到底约束了哪个契约(而不是先去改看起来可疑的那行),"
        "再确认修复点是不是应该在调用方/被调方边界处收口。不要重复上一轮的改动。"
    ),
    1: (
        "## 本轮要求:换思路(方向 B)\n"
        "上一轮的同一种改法没有修好。请从失败路径入手:列举这条断言还可能被哪些"
        "分支/边界输入影响(空值、越界、类型、顺序、异常),并把补丁做在缺少的"
        "那个边界处理上。不要重复上一轮的改动。"
    ),
}


@dataclass
class TaskNodes:
    """一个任务一次图执行的节点集合(闭包状态,不进 LangGraph state)。"""

    bug: BugTask
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
    # M6 崩溃恢复:被打断阶段的循环快照(仅当 stage/round 与该阶段本次执行匹配时消费一次,
    # 取走即清空——同一份快照不得被后续阶段或后续轮次再回放一遍)。默认 None = 冷启动,
    # 与引入恢复能力之前的行为逐字一致。
    resume_snapshot: LoopSnapshot | None = None

    def _take_resume_snapshot(self, stage: str, round_no: int) -> LoopSnapshot | None:
        """按"当前阶段+当前轮次"领取快照;不匹配返回 None(冷启动)。"""
        snapshot = self.resume_snapshot
        if snapshot is None:
            return None
        if snapshot.stage != stage or snapshot.round_no != round_no:
            return None
        self.resume_snapshot = None  # 一次性:领取后不再出现在任何后续循环里
        return snapshot

    # ---------- CREATED ----------

    def _build_ctx(self, state: TaskState) -> ToolContext:
        """ToolContext 的唯一构造口径(prepare 与崩溃恢复共用,防两份实现漂移)。"""
        settings = get_settings()
        return ToolContext(
            task_id=state["bug_id"],
            workspace=self.workspace,
            baseline_commit=self.baseline_commit,
            tracker=self.tracker,
            report_dir=self.report_dir,
            env=self.bug.env,
            test_sets=self.bug.test_sets,
            allowed_paths=self.bug.allowed_paths,
            max_patch_files=settings.max_patch_files,
            test_timeout_seconds=settings.test_timeout_seconds,
            # P1-4 整改:资源上限此前只落在硬编码默认值上,settings 改了不生效
            max_read_lines=settings.max_read_lines,
            max_search_results=settings.max_search_results,
        )

    def restore_runtime(self, state: TaskState, baseline_commit: str) -> None:
        """M6 崩溃恢复:重建 prepare 造出的闭包对象(ctx / baseline_commit)。

        恢复时图从 checkpoint 的 `next` 节点起跑,prepare/baseline 不会重跑,而这两个
        对象按设计**不进 state**(不可序列化)——不重建它们,第一个节点就会
        `assert self.ctx is not None` 判死。基线取 state 里的完整 sha,缺失(旧检查点)
        时由调用方回退工作区 HEAD。
        """
        self.baseline_commit = baseline_commit
        self.ctx = self._build_ctx(state)

    def prepare(self, state: TaskState) -> dict[str, Any]:
        """物化题目仓库为 git 工作区,固定基线 commit。"""
        try:
            self.baseline_commit = materialize_repo(
                self.bug.repo_dir, self.workspace, extra_commit=False
            )
            self.ctx = self._build_ctx(state)
            self.tracker.record(
                tool="create_workspace",
                state="BASELINE",
                input_payload={"baseline": self.baseline_commit[:12]},
            )
            # M6:基线 sha 进 state(SqliteSaver 可序列化),恢复方据此复位工作区
            return {
                "status": "BASELINE",
                "baseline_regression_ok": False,
                "baseline_commit": self.baseline_commit,
            }
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
            env=self.ctx.env,
        )
        regression_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.regression_tests,
            self.report_dir / "baseline-regression.xml",
            env=self.ctx.env,
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

    # ---------- 持久记忆(M2 仓库骨架) ----------

    def _persistent_context(self, workspace: Path) -> str:
        """LOCALIZE/PROPOSE 共用的仓库骨架:纯本地 ast 生成、零 LLM 请求、每阶段一次。

        缺陷证据(PROGRESS.md D.4/D.11):结构从未进过提示,模型只能自己 list_files 现场
        重建——真实多文件题光靠 read/search 就烧穿定位段份额(runs/swe-hard-graph*:
        16-19 轮、417,894 tokens、apply_patch 0 次)。骨架先验把"重新发现仓库"换成"直接精读"。
        返回值只作为 extra_system 附加文本:allowed_tools/token_budget/BudgetError/
        TaskCancelled 的路径一律不碰;生成失败退化为空串,可选上下文不得带走任务。
        getattr 兜底让只构造部分字段的调用方与测试桩落在"关闭"一侧,
        此时 extra_system 与本改动前逐字一致(回归钉子见 tests/test_repo_map.py)。
        """
        settings = get_settings()
        if not getattr(settings, "repo_map_enabled", False):
            return ""
        return repo_map_for_workspace(
            workspace,
            max_chars=getattr(settings, "repo_map_max_chars", 0),
            max_files=getattr(settings, "repo_map_max_files", 200),
            dir_depth=getattr(settings, "repo_map_dir_depth", 3),
        )

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
                # 主线 localize 无阶段提示,故 extra_system 即骨架(候选点未接,见 _run_candidate)
                extra_system=self._persistent_context(self.workspace),
                allowed_tools=READ_TOOLS,
                started_monotonic=self.started_monotonic,
                time_budget_seconds=get_settings().task_timeout_seconds,
                token_budget=self._token_budget_for(
                    state, share=get_settings().localize_budget_share
                ),
                context_window_tokens=get_settings().context_window_tokens,
                context_keep_recent_turns=get_settings().context_keep_recent_turns,
                cancel_event=self.cancel_event,
                resume_snapshot=self._take_resume_snapshot("LOCALIZE", state["round_no"]),
            )
        except BudgetError as exc:
            # 两种"耗尽"要分开看,否则会毁掉任务:
            # - 任务级总额已超 → 仍是终点(N-5:不得带着已花的钱继续 propose 绕资源门禁);
            # - 只是定位段的份额/轮次用尽,任务级还有余量 → **降级继续**:拿模型最后一轮
            #   的结论当暂定 findings 进补丁阶段。实测难题档 4 次失败全是"只读调查花光额度、
            #   apply_patch 一次都没发生",终止等于必然 0 产出。
            # N-11 整改:两种情形都把循环已烧的 token/turns 记回任务级账本,不蒸发
            spent = exc.tokens_spent
            usage = {
                "turns": state.get("turns", 0) + exc.turns,
                "tokens_used": state.get("tokens_used", 0) + spent,
                "tokens_prompt": state.get("tokens_prompt", 0) + exc.tokens_prompt,
                "tokens_completion": state.get("tokens_completion", 0) + exc.tokens_completion,
            }
            budget = get_settings().token_budget
            if budget > 0 and usage["tokens_used"] >= budget:
                return {
                    **usage,
                    "status": "BUDGET_EXCEEDED",
                    "outcome": "failed",
                    "error": f"localize: {exc}",
                }

            provisional = self._provisional_findings(exc.last_content or "")
            self.tracker.record(
                tool="localize_degraded",
                state="LOCALIZE",
                input_payload={"task_token_budget": budget, "tokens_used": usage["tokens_used"]},
                output_summary={
                    "reason": "max_turns" if "max_turns" in str(exc) else "phase_share",
                    "findings_chars": len(provisional),
                },
            )
            return {
                **usage,
                "status": "PROPOSE_PATCH",
                "findings": provisional,
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

    def _provisional_findings(self, last_content: str) -> str:
        """额度耗尽时的暂定结论:模型最后一轮的实质文本 + 已调查过的线索清单。

        为什么不止"最后一轮文本":实测 sphinx-7590 拿 63 字符的半程结论冷启动补丁阶段后,
        又 search/read 了 17 次仍未提交(0 次 apply_patch)就撞任务级总额。把"定位阶段已经查过
        什么"带进提示,补丁阶段才能从"接着写"开始而不是"重新查"。纯本地拼接,不多花一次请求。

        清单按出现次数降序、次数相同按首次出现顺序(确定性,回放可比);每项截断,总量封顶。
        """
        buckets: dict[str, dict[str, int]] = {"read_file": {}, "search_code": {}, "list_files": {}}
        seen: dict[str, int] = {}
        for ev in self.tracker.events:
            if ev.state != "LOCALIZE" or ev.tool not in buckets:
                continue
            args = ev.input or {}
            key = str(
                args.get("path")
                or args.get("keyword")
                or args.get("subdir")
                or args.get("glob")
                or ""
            ).strip()
            if not key:
                continue
            key = key[:80]
            counts = buckets[ev.tool]
            counts[key] = counts.get(key, 0) + 1
            seen.setdefault(key, len(seen))

        lines = [
            last_content or "(定位未在额度内收敛;以下为已调查线索,请先确认根因再按块协议提交补丁)"
        ]
        rendered = False
        for tool, counts in buckets.items():
            if not counts:
                continue
            top = sorted(counts.items(), key=lambda kv: (-kv[1], seen.get(kv[0], 0)))[:8]
            lines.append(f"- {tool}: " + ", ".join(f"{k} ×{c}" for k, c in top))
            rendered = True
        if rendered:
            lines.insert(1, "定位阶段已调查线索(未收敛,仅作起点):")
        return "\n".join(lines)

    def _token_budget_for(self, state: TaskState, share: float = 1.0) -> int | None:
        """本循环可用的 token 余量(N-11 整改)。

        此前每个循环都各自拿满 Settings.token_budget——localize 烧满后 propose
        又是全新一份,任务级真实消耗可达配置的数倍。None 表示任务级不限制。
        调用方需先处理余量已耗尽的情形(0 会被 run_plain_loop 当作"不限制")。

        share < 1 时给该段只切一部分余量(LOCALIZE 用它,把剩余留给补丁阶段);
        分支与候选的额度口径不受影响——它们本来就按整份余量做前置判断。
        """
        settings = get_settings()
        if settings.token_budget <= 0:
            return None
        remaining = max(settings.token_budget - state.get("tokens_used", 0), 1)
        if share >= 1.0:
            return remaining
        return max(int(remaining * share), 1)

    # ---------- PLAN ----------

    def plan(self, state: TaskState) -> dict[str, Any]:
        """PLAN 阶段:把定位结论固化成一份跨轮可修订的计划工件(LOCALIZE→PLAN→ACT→VERIFY)。

        缺陷证据(PROGRESS.md D.8/D.11):此前**根本没有计划这一步**——"计划如何修复"只是
        LOCALIZE_PROMPT 要求塞进 finish.summary 的自由文本(app/prompts.py:57),不是工件。
        付费实跑 runs/swe-hard-graph/3 两次独立跑法同形:PROPOSE 13 次 search / 10 次 read、
        apply_patch **0 次**——它拿着一份单薄的定位摘要,于是重新调查而不是动手改。
        本节点的产出进 state(可 checkpoint),失败轮由 apply 重试边/rollback 边回到这里
        **修订同一工件**,而不是让下一轮 PROPOSE 冷启动再猜一遍。

        刻意不做的两件事:
        ①不给任何检索/写入工具——"先计划"不能变成"再调查一轮";结构信息走骨架注入
          (与 localize/propose 同一份持久记忆:计划要点名文件与符号,而实测定位结论
          常薄到 63-397 字符,只靠它就只能写出"改那个模块"这类无法执行的话);
        ②降级不等于失败——计划段份额用完只带暂定文本进 PROPOSE(与 localize 同构),
          任务级总额耗尽仍是硬终点,N-5 的处置一字不改。
        """
        assert self.ctx is not None
        settings = get_settings()
        if not getattr(settings, "plan_stage_enabled", True):
            # 关闭即回到旧行为:零 LLM 请求、零轨迹事件、state.plan 恒为空串,
            # 于是 PROPOSE 的渲染与引入本阶段之前逐字节相同
            return {"plan": ""}

        prompt = build_plan_prompt(
            issue_text=state["issue_text"],
            findings=state.get("findings", ""),
            feedback=state.get("feedback", ""),
            previous_plan=state.get("plan", ""),
            round_no=state["round_no"],
        )
        try:
            outcome = run_plain_loop(
                self.ctx,
                self.model,
                prompt,
                max_turns=self.max_turns,
                round_no=state["round_no"],
                state_label="PLAN",
                extra_system=self._persistent_context(self.workspace),
                # 计划只产文本:工具白名单只留 finish,不给任何检索/写入面
                # (否则"先计划"会变成"再调查一轮")
                allowed_tools=[FINISH_TOOL],
                started_monotonic=self.started_monotonic,
                time_budget_seconds=settings.task_timeout_seconds,
                token_budget=self._token_budget_for(
                    state, share=getattr(settings, "plan_budget_share", 0.15)
                ),
                context_window_tokens=settings.context_window_tokens,
                context_keep_recent_turns=settings.context_keep_recent_turns,
                cancel_event=self.cancel_event,
                resume_snapshot=self._take_resume_snapshot("PLAN", state["round_no"]),
            )
        except BudgetError as exc:
            # 两类"耗尽"的处置与 localize 同构:任务级总额已超 → 终点;
            # 只是计划段的份额/轮次用尽 → 拿模型最后一轮的实质文本当暂定计划继续。
            # N-11 同源:循环已经烧掉的 token/turns 必须记回任务级账本,不蒸发
            usage = {
                "turns": state.get("turns", 0) + exc.turns,
                "tokens_used": state.get("tokens_used", 0) + exc.tokens_spent,
                "tokens_prompt": state.get("tokens_prompt", 0) + exc.tokens_prompt,
                "tokens_completion": state.get("tokens_completion", 0) + exc.tokens_completion,
            }
            budget = settings.token_budget
            if budget > 0 and usage["tokens_used"] >= budget:
                return {
                    **usage,
                    "status": "BUDGET_EXCEEDED",
                    "outcome": "failed",
                    "error": f"plan: {exc}",
                }

            provisional = (exc.last_content or "").strip()
            self.tracker.record(
                tool="plan_degraded",
                state="PLAN",
                input_payload={"task_token_budget": budget, "tokens_used": usage["tokens_used"]},
                output_summary={
                    "reason": "max_turns" if "max_turns" in str(exc) else "phase_share",
                    "plan_chars": len(provisional),
                },
            )
            return {**usage, "status": "PROPOSE_PATCH", "plan": provisional}
        except TaskCancelled:
            raise  # 交给 runner 收敛为 CANCELLED,不得吞成 NEEDS_REVIEW
        except Exception as exc:
            return {
                "status": "NEEDS_REVIEW",
                "outcome": "needs_review",
                "error": f"plan: {exc}",
            }

        return {
            "status": "PROPOSE_PATCH",
            "plan": outcome.summary,
            "turns": state.get("turns", 0) + outcome.turns,
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
            "tokens_prompt": state.get("tokens_prompt", 0) + outcome.tokens_prompt,
            "tokens_completion": state.get("tokens_completion", 0) + outcome.tokens_completion,
        }

    def route_plan(self, state: TaskState) -> str:
        """终点只有 NEEDS_REVIEW / BUDGET_EXCEEDED 两条(与 route_localize 同口径)。

        计划段自己的份额耗尽**不是**终点:route_apply/route_rollback 的重试边如今指向
        本节点,若把降级判成终止,一道"计划没写全"就能把整题判死,比旧行为更糟。
        """
        return "end" if state["status"] in ("NEEDS_REVIEW", "BUDGET_EXCEEDED") else "continue"

    # ---------- PROPOSE_PATCH ----------

    def _propose_prompt(self, state: TaskState, round_no: int) -> str:
        """PROPOSE 阶段的完整提示(P1-1:全新会话必须带全 Bug 描述与定位结论)。

        分支候选与主线共用这一份构造——候选不比主线多看任何东西,只是换了思路提示。
        M5:计划块按**追加**方式拼接,plan 为空时渲染结果与引入 PLAN 阶段之前逐字节相同。
        """
        return PROPOSE_PROMPT.format(
            round_no=round_no,
            issue_text=state["issue_text"],
            findings=state.get("findings") or "(定位阶段未给出结论;请先用只读工具确认根因)",
            feedback=state.get("feedback", ""),
        ) + plan_block_for_propose(state.get("plan", ""))

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

        prompt = self._propose_prompt(state, state["round_no"])
        try:
            outcome = run_plain_loop(
                self.ctx,
                self.model,
                prompt,
                max_turns=self.max_turns,
                round_no=state["round_no"],
                state_label="PROPOSE_PATCH",
                extra_system=self._persistent_context(self.workspace),
                allowed_tools=WRITE_TOOLS,
                started_monotonic=started,
                time_budget_seconds=get_settings().task_timeout_seconds,
                token_budget=self._token_budget_for(state),
                context_window_tokens=get_settings().context_window_tokens,
                context_keep_recent_turns=get_settings().context_keep_recent_turns,
                cancel_event=self.cancel_event,
                resume_snapshot=self._take_resume_snapshot("PROPOSE_PATCH", state["round_no"]),
            )
        except BudgetError as exc:
            # N-5 整改:超预算必须终止(route_propose 会 end),不得带着已应用
            # 的补丁继续 apply/verify 把资源门禁绕过去。
            # N-11 整改:已耗 token/turns 记回任务级账本
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": f"propose: {exc}",
                "turns": state.get("turns", 0) + exc.turns,
                "tokens_used": state.get("tokens_used", 0) + exc.tokens_spent,
                "tokens_prompt": state.get("tokens_prompt", 0) + exc.tokens_prompt,
                "tokens_completion": state.get("tokens_completion", 0) + exc.tokens_completion,
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
            # 转移表:PATCH_REJECTED 且轮数未超 → 回 PLAN 重规划(M5)再进 PROPOSE;重试计入轮数
            update["round_no"] = state["round_no"] + 1
        return update

    def route_apply(self, state: TaskState) -> str:
        if state["status"] == "VERIFY":
            return "verify"
        # PATCH_REJECTED:轮数未超 → 回 PLAN 重规划(builder 里 retry→plan);
        # 超了 → BUDGET_EXCEEDED(企划书 4.2)
        if state["round_no"] < self.max_rounds:
            return "retry"
        return "exhausted"

    # ---------- VERIFY ----------

    def _deadline_overrun(self) -> str | None:
        """任务级 deadline 复查(复盘 P1-6):verify 双跑最多 4 次 pytest,单次各有
        test_timeout,但都不查任务级 task_timeout_seconds——不复查则可整体越界。
        started_monotonic 未接线(直连 TaskNodes 的测试场景)不查,与 R2 同口径;
        verify 无模型调用不耗 token,任务级 token 预算由 propose/apply 段守卫。"""
        settings = get_settings()
        if self.started_monotonic is None or settings.task_timeout_seconds <= 0:
            return None
        elapsed = time.monotonic() - self.started_monotonic
        if elapsed > settings.task_timeout_seconds:
            return (
                f"verify: task exceeded time budget {settings.task_timeout_seconds}s"
                f" (elapsed {elapsed:.0f}s)"
            )
        return None

    def verify(self, state: TaskState) -> dict[str, Any]:
        assert self.ctx is not None
        # 复盘 P1-6:每次测试执行前复查任务级 deadline,超限以 BUDGET_EXCEEDED
        # 终态收尾(与 localize/propose 段同语义),不得越过 task_timeout_seconds;
        # 已拿到的部分结果随状态带回,供事后复盘
        if (overrun := self._deadline_overrun()) is not None:
            return {"status": "BUDGET_EXCEEDED", "outcome": "failed", "error": overrun}
        failed_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.failed_tests,
            self.report_dir / "verify-failed.xml",
            env=self.ctx.env,
        )
        if (overrun := self._deadline_overrun()) is not None:
            return {
                "status": "BUDGET_EXCEEDED",
                "outcome": "failed",
                "error": overrun,
                "verify_failed_ok": failed_report.all_passed,
            }
        regression_report, _ = run_pytest(
            self.ctx.python_exe,
            self.workspace,
            self.bug.regression_tests,
            self.report_dir / "verify-regression.xml",
            env=self.ctx.env,
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
            # 双跑前再查一次:这是 verify 段最贵的追加开销(2 次全量 pytest),
            # 任务级 deadline 已到时不为复核越过 task_timeout_seconds
            if (overrun := self._deadline_overrun()) is not None:
                return {
                    "status": "BUDGET_EXCEEDED",
                    "outcome": "failed",
                    "error": overrun,
                    "verify_failed_ok": failed_report.all_passed,
                    "verify_regression_ok": regression_report.all_passed,
                }
            rerun_failed, _ = run_pytest(
                self.ctx.python_exe,
                self.workspace,
                self.bug.failed_tests,
                self.report_dir / "verify-failed-rerun.xml",
                env=self.ctx.env,
            )
            rerun_regression, _ = run_pytest(
                self.ctx.python_exe,
                self.workspace,
                self.bug.regression_tests,
                self.report_dir / "verify-regression-rerun.xml",
                env=self.ctx.env,
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
        if state["status"] == "BUDGET_EXCEEDED":
            # 复盘 P1-6:verify 中途撞任务级 deadline → 终点(与 localize 的 end 同构)
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

    def _run_candidate(self, state: TaskState, index: int, reserve: int | None) -> BranchCandidate:
        """一个候选:隔离工作区 → 换思路跑一轮 propose → 双测试集自验 → 取工作区 diff。

        隔离用 `materialize_repo`(与主工作区同源、各自独立 git),不用 git worktree:
        worktree 共享 object 库且要清理注册项,题目仓库本就是"复制一份"的模型,
        复制更简单也没有跨工作区耦合。
        """
        assert self.ctx is not None
        settings = get_settings()
        cand_ws = self.report_dir / f"workspace-cand{index}"
        cand_reports = self.report_dir / f"cand{index}"
        cand_reports.mkdir(parents=True, exist_ok=True)
        sha = materialize_repo(self.bug.repo_dir, cand_ws, extra_commit=False)
        cand_ctx = ToolContext(
            task_id=f"{self.ctx.task_id}-cand{index}",
            workspace=cand_ws,
            baseline_commit=sha,
            tracker=Tracker(cand_reports / "trajectory.jsonl", task_id=f"cand{index}"),
            report_dir=cand_reports,
            env=self.bug.env,
            python_exe=self.ctx.python_exe,
            test_sets=self.ctx.test_sets,
            allowed_paths=self.ctx.allowed_paths,
            whitelist=self.ctx.whitelist,
            max_read_lines=self.ctx.max_read_lines,
            max_search_results=self.ctx.max_search_results,
            max_patch_files=self.ctx.max_patch_files,
            test_timeout_seconds=self.ctx.test_timeout_seconds,
            forbid_test_files=self.ctx.forbid_test_files,
        )
        rounds = state["round_no"] + 1
        note = ""
        outcome = LoopOutcome(
            success=False,
            summary="",
            turns=0,
            tokens_used=0,
            patch_applied=False,
            finish_declared=False,
        )
        try:
            outcome = run_plain_loop(
                cand_ctx,
                self.branch_model_factory(index),  # type: ignore[misc]
                self._propose_prompt(state, rounds),
                max_turns=self.max_turns,
                round_no=rounds,
                state_label=f"BRANCH-{index}",
                extra_system=VARIANT_HINTS.get(index, ""),
                allowed_tools=WRITE_TOOLS,
                # 候选各拿任务级余量的一半:两个候选合计不超一轮双倍开销。
                # max(…, 1):余量极小时 0 会被 plain_loop 当"不限制",
                # 脚枪式语义(复盘 P2);至少给 1,让候选在首个 turn 边界即被预算拦下
                token_budget=max(reserve // 2, 1) if reserve else reserve,
                started_monotonic=self.started_monotonic,
                time_budget_seconds=settings.task_timeout_seconds,
                context_window_tokens=settings.context_window_tokens,
                context_keep_recent_turns=settings.context_keep_recent_turns,
                cancel_event=self.cancel_event,
            )
        except TaskCancelled:
            raise  # 取消语义不得被候选吞掉
        except BudgetError as exc:
            note = f"预算超限:{exc}"
            # 超预算的候选也要记账:已花的 token/turn 是真实成本,不能因失败而抹掉
            outcome = LoopOutcome(
                success=False,
                summary=note,
                turns=exc.turns,
                tokens_used=exc.tokens_spent,
                patch_applied=False,
                finish_declared=False,
                tokens_prompt=exc.tokens_prompt,
                tokens_completion=exc.tokens_completion,
            )
        except Exception as exc:  # 候选失败不拖垮主流程:按"未修好"计分
            note = f"{type(exc).__name__}: {exc}"

        diff = working_tree_diff(cand_ws)
        failed_report, _ = run_pytest(
            cand_ctx.python_exe,
            cand_ws,
            self.bug.failed_tests,
            cand_reports / "branch-failed.xml",
            timeout_seconds=settings.test_timeout_seconds,
            env=cand_ctx.env,
        )
        regression_report, _ = run_pytest(
            cand_ctx.python_exe,
            cand_ws,
            self.bug.regression_tests,
            cand_reports / "branch-regression.xml",
            timeout_seconds=settings.test_timeout_seconds,
            env=cand_ctx.env,
        )
        # 候选阶段不做 E3 双跑:双跑复核属于主流程的 resolved 判定,择优阶段
        # 多一倍 pytest 开销换来的只是"同一伪绿再验一次",与择优目的无关
        return BranchCandidate(
            index=index,
            workspace=cand_ws,
            diff_text=diff.diff_text,
            failed_ok=failed_report.all_passed,
            regression_ok=regression_report.all_passed,
            failed_remaining=failed_report.failed + failed_report.errors,
            failed_cases=[
                {"name": c.test_name, "signature": c.signature}
                for c in [*failed_report.failed_cases, *regression_report.failed_cases]
            ],
            turns=outcome.turns,
            tokens_used=outcome.tokens_used,
            tokens_prompt=outcome.tokens_prompt,
            tokens_completion=outcome.tokens_completion,
            error=note,
        )

    @staticmethod
    def _select_candidate(cands: list[BranchCandidate]) -> BranchCandidate:
        """择优:先要"双集通过",再要剩余失败少、报告失败少、花得省。"""
        return max(
            cands,
            key=lambda c: (
                c.failed_ok,
                c.regression_ok,
                -c.failed_remaining,
                -len(c.failed_cases),
                -c.tokens_used,
            ),
        )

    def _run_fallback_branches(self, state: TaskState) -> dict[str, Any]:
        """触发一次 Best-of-N:并行候选 → 择优 → 胜者补丁合流回主工作区。

        合流后走 **既有 apply 节点**做图级门禁复核(apply 会重读工作区 diff 跑
        run_gates),门禁链对分支产物照常全量执行;verify 再判一次,判定规则未变。
        本任务至多分支一次(branching_used)。
        """
        assert self.ctx is not None and self.branch_model_factory is not None
        settings = get_settings()
        round_no = state["round_no"]
        reason = (
            f"repeat_streak={state.get('repeat_streak', 0)},"
            f" patch_fail_streak={self.ctx.patch_fail_streak}"
        )
        self.tracker.record(
            tool="adaptive_branch_trigger",
            round_no=round_no,
            state="PROPOSE_PATCH",
            input_payload={"round": round_no, "reason": reason},
        )
        reserve = self._token_budget_for(state) or 0
        count = max(2, settings.branch_candidates)
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(self._run_candidate, state, i, reserve) for i in range(count)]
            cands = [f.result() for f in futures]  # TaskCancelled 在此向上抛出

        winner = self._select_candidate(cands)
        self.tracker.record(
            tool="adaptive_branch_selected",
            round_no=round_no,
            state="PROPOSE_PATCH",
            output_summary={
                "winner": winner.index,
                "candidates": [
                    {
                        "index": c.index,
                        "failed_ok": c.failed_ok,
                        "regression_ok": c.regression_ok,
                        "remaining": c.failed_remaining,
                        "error": c.error,
                    }
                    for c in cands
                ],
            },
        )
        spent_turns = sum(c.turns for c in cands)
        spent_tokens = sum(c.tokens_used for c in cands)
        account = {
            "turns": state.get("turns", 0) + spent_turns,
            "tokens_used": state.get("tokens_used", 0) + spent_tokens,
            "tokens_prompt": state.get("tokens_prompt", 0) + sum(c.tokens_prompt for c in cands),
            "tokens_completion": state.get("tokens_completion", 0)
            + sum(c.tokens_completion for c in cands),
        }
        if not winner.diff_text.strip():
            # 候选都没产出可应用的改动:回到单线下一轮,不烧门禁与 pytest
            self.tracker.record(
                tool="adaptive_branch_no_patch",
                round_no=round_no,
                state="PROPOSE_PATCH",
                error="所有候选都没有产生工作区改动",
            )
            return {
                **account,
                "status": "PROPOSE_PATCH",
                "round_no": round_no + 1,
                "branching_used": True,
                "verify_failed_ok": False,
                "verify_regression_ok": False,
                "feedback": build_feedback(
                    winner.failed_cases,
                    extra_note="自适应分支:两个候选都没有产出可应用的补丁。",
                ),
            }

        applied = git_apply_patch(self.workspace, winner.diff_text)
        if not applied.applied:
            # 主工作区已 reset 在与候选同源的位置,理论上可干净应用;真失败就退回单线
            return {
                **account,
                "status": "PROPOSE_PATCH",
                "round_no": round_no + 1,
                "branching_used": True,
                "verify_failed_ok": False,
                "verify_regression_ok": False,
                "feedback": (
                    f"自适应分支胜者(候选 {winner.index})的补丁合流失败"
                    f"({applied.rejected_reason}):{applied.detail}——请自行重写补丁"
                ),
            }

        if winner.failed_ok and winner.regression_ok:
            feedback = (
                f"自适应分支已择优:候选 {winner.index} 在其隔离工作区内双测试集通过,"
                "补丁已合流到主工作区,下面按正常流程做图级门禁复核与验证。"
            )
        else:
            # 候选全败也合流"最不差"的那个:让下一轮基于真实失败反馈继续,而不是空转
            feedback = build_feedback(
                winner.failed_cases,
                extra_note=(
                    f"自适应分支的 {count} 个候选都未完全修好;"
                    f"取剩余失败最少的候选 {winner.index} 合流,请据此换方向。"
                ),
            )
        return {
            **account,
            "status": "APPLY_PATCH",
            "round_no": round_no + 1,
            "branching_used": True,
            "branch_selected": winner.index,
            "patch_fail_streak": self.ctx.patch_fail_streak,
            "feedback": feedback,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
        }

    def rollback(self, state: TaskState) -> dict[str, Any]:
        # N-12 整改:回滚会 reset 掉工作区,末轮(验证失败/门禁拒绝)的直接证据
        # 必须先取下——否则 runner 落盘的 diff.patch 是回滚后的空 diff,取证现场被毁
        from app.gitops.differ import working_tree_diff
        from app.gitops.rollback import reset_workspace
        from app.graph.reflection import with_discarded_patch

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
        # 卡5b:轮数耗尽优先终止(上面已 return),之后才考虑分支一次
        if self._should_branch(state):
            return self._run_fallback_branches(state)
        return {
            "status": "PROPOSE_PATCH",
            "round_no": state["round_no"] + 1,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
            # M4 失败反思:工作区已被复位,下一轮 PROPOSE 是新会话,除了失败用例还得知道
            # "上一轮到底改了什么、那条路已经不通"——否则 repeat_streak(同一组失败连续多轮)
            # 只会以"再写一遍同样的假设"的形式重现。只给形状(文件/增删行数/hunk 上下文),
            # 不给补丁正文:正文会诱导模型逐字重放上一版。
            "feedback": with_discarded_patch(state.get("feedback", ""), preserved),
        }

    def route_rollback(self, state: TaskState) -> str:
        """BUDGET_EXCEEDED → 终点;APPLY_PATCH(分支合流)→ apply 节点做图级门禁复核;
        其余 → 下一轮重规划(M5:返回串仍是既有的 "propose" 键名,builder 把它接到 plan 节点,
        这样状态串与本函数返回值都不动,只有节点目标改变)。"""
        if state["status"] == "BUDGET_EXCEEDED":
            return "end"
        if state["status"] == "APPLY_PATCH":
            return "apply"
        return "propose"
