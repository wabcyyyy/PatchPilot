"""崩溃恢复的 B 级接线:把 SqliteSaver 检查点读回可用的运行位置(M6/S03)。

分工(两级恢复各管一段,缺一不可):
- **A 级**(`app/graph/loop_state.py`):Agent 循环的工作记忆(messages / token 累计 /
  轮次),因为 LangGraph 只在 superstep **之间**打检查点,一个阶段内部跑到第 19 轮崩溃时
  检查点里根本没有这 19 轮——没有 A 级,B 级只能把整个阶段冷启动重跑一遍。
- **B 级**(本模块 + `runner.run_task_graph(resume=True)`):图状态与"接下来该跑哪个节点",
  由 `graph.get_state()` 读回。

S03(ADR-0009 §4)起 prepare_resume 返回**结构化 ResumeDecision**,
runner 不再把恢复失败默认为冷启动整个任务(冷启动会把时间/循环/token 额度
整份重发一遍):

| checkpoint next | 决定 |
|---|---|
| localize/plan | continue:校验任务/快照/资源后续跑;只读现场不 reset |
| propose | continue:既有循环快照续跑,写阶段先 reset;快照不完整时该阶段冷启动 |
| apply/verify/finish | revalidate:验证冻结候选与身份→reset→重应用候选→清旧 verify/gate 结论→从 APPLY 完整重验 |
| rollback | continue:失败候选在 apply 入口已冻结,回滚节点自己做保全+reset |
| END | already_terminal:只核对既有终态产物,不冷启动同一任务再次收费 |

两条安全前提(架构决定,不可协商):
1. **工作区完整性**:被打断的阶段若能写文件,续跑前必须先把工作区复位到基线。
   块补丁协议的锚点是拿**磁盘上的实际内容**校验的,崩溃残留的半截补丁会让续跑的
   补丁落到错误位置或静默错位。revalidate 路径的复位与重应用一体完成;
   propose 的复位先于循环快照回放;rollback **不复位**——回滚节点要先保全
   现场再自己复位,提前复位会把 preserved_diff 取证毁成空 diff。
2. **门禁必须重跑**:恢复只带回升阶时的**工作记忆**,绝不带回任何**判断**。
   revalidate 显式清掉旧的 verify/gate 布尔;apply 门禁、verify 双测试集、
   E3 双跑一致性、最终 verdict 全部在续跑的那次执行里真实重算。

候选缺失/篡改、源码身份不符、基线不符、无可信 deadline、无剩余步数/余额:
一律 reject(NEEDS_REVIEW 或明确预算终止),不伪成功;拒绝决定先于任何复位,
被拒绝的恢复不得先毁掉崩溃现场。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.gitops.cmd import run_git
from app.gitops.patcher import check_patch_targets
from app.gitops.rollback import reset_workspace
from app.graph.candidate import (
    accepted_contract_hashes,
    load_candidate,
    reapply_candidate,
)
from app.graph.loop_state import load_loop_snapshot

if TYPE_CHECKING:
    from app.graph.nodes import TaskNodes

log = logging.getLogger(__name__)

# 跑 run_plain_loop 的节点 → 阶段标签(只有这些阶段留有工作记忆快照可续)
LOOP_NODE_STAGES: dict[str, str] = {
    "localize": "LOCALIZE",
    "plan": "PLAN",
    "propose": "PROPOSE_PATCH",
}
# revalidate 适用面:补丁交接点之后的任何位置恢复,都要按候选重应用+完整重验
REVALIDATE_NODES: frozenset[str] = frozenset({"apply", "verify", "finish"})
# 续跑前必须先复位的节点:只有 propose(apply/verify/finish 由 revalidate 一体完成;
# rollback 刻意不复位——回滚节点要先保全现场再自己复位,提前复位会毁掉 preserved_diff)
PRE_RESET_NODES: frozenset[str] = frozenset({"propose"})


@dataclass
class ResumeDecision:
    """恢复决定;runner 按 action 分流,绝不在恢复失败时默认冷启动。"""

    action: str  # continue | revalidate | reject | already_terminal
    config: dict[str, Any] | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _workspace_head(workspace: Path) -> str:
    """旧检查点缺 baseline_commit 字段时的回退:工作区 HEAD。

    Agent 从不提交(补丁只改工作树),所以 HEAD 就是基线;拿不到时返回空串,
    由调用方按"无法复位"处理——不做任何猜测性 reset。
    """
    if not workspace.is_dir():
        return ""
    try:
        rc, out, _ = run_git(workspace, "rev-parse", "HEAD", check=False)
    except Exception as exc:  # git 缺失/超时:降级为"不复位",由快照阶段照常续跑
        log.warning("resume: cannot read workspace HEAD (%s)", exc)
        return ""
    return out.strip() if rc == 0 else ""


def _read_final_report(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "report.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prepare_resume(
    nodes: TaskNodes,
    graph: Any,
    *,
    run_dir: Path,
    config: dict[str, Any],
    task_id: str,
) -> ResumeDecision:
    """读检查点并决定恢复方式;绝不抛——所有失败都是结构化 reject。"""
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:
        # 无 checkpointer 的图会在这里报错:恢复能力依赖检查点
        log.warning("task %s: cannot read checkpoint state (%s)", task_id, exc)
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"reason": "get_state failed"},
            error=f"{type(exc).__name__}: {exc}",
        )
        return ResumeDecision(action="reject", reason="checkpoint unreadable")

    values: dict[str, Any] = dict(snapshot.values or {})
    pending: tuple[str, ...] = tuple(snapshot.next or ())
    checkpoint_id = str((snapshot.config or {}).get("configurable", {}).get("checkpoint_id") or "")
    if not values:
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"reason": "no checkpoint values"},
            error="no resumable position in checkpoint",
        )
        return ResumeDecision(action="reject", reason="no checkpoint values")

    if not pending:
        # END:收尾曾经完成。只核对终态产物一致性,不冷启动同一任务再次收费。
        report = _read_final_report(Path(run_dir))
        if report is None:
            return ResumeDecision(
                action="reject", reason="terminal checkpoint without report artifacts"
            )
        nodes.tracker.record(
            tool="resume_terminal_replayed",
            state="RESUME",
            input_payload={"checkpoint_id": checkpoint_id[:12]},
            output_summary={
                "status": report.get("status"),
                "verdict": report.get("verdict"),
                "tokens_used": report.get("tokens_used"),
            },
        )
        return ResumeDecision(
            action="already_terminal",
            detail={"report": report, "checkpoint_id": checkpoint_id},
        )

    node = pending[0]
    stage = LOOP_NODE_STAGES.get(node, "")
    round_no = int(values.get("round_no", 1) or 1)

    # 递归额度先判,再动任何东西(顺序是安全要求,不是风格):
    # 一个最终被拒绝的续跑不该先把崩溃现场抹掉。
    step = int((snapshot.metadata or {}).get("step", 0) or 0)
    original_limit = int(config.get("recursion_limit", 50))
    remaining_limit = original_limit - max(step, 0)
    if remaining_limit <= 0:
        log.warning(
            "task %s: recursion budget already spent (step=%s limit=%s)",
            task_id,
            step,
            original_limit,
        )
        nodes.tracker.record(
            tool="resume_unavailable",
            round_no=round_no,
            state="RESUME",
            input_payload={"next_node": node, "step": step, "limit": original_limit},
            error="recursion budget exhausted before resume",
        )
        return ResumeDecision(action="reject", reason="recursion budget exhausted")

    # S02b(ADR-0009 §3):恢复只顺延原 deadline 的剩余时间,绝不重授。
    raw_deadline = values.get("deadline_epoch")
    try:
        persisted_deadline = float(raw_deadline) if raw_deadline is not None else None
    except (TypeError, ValueError):
        persisted_deadline = None
    if persisted_deadline is None:
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"next_node": node, "reason": "no persisted deadline_epoch"},
            error="checkpoint has no trusted deadline; resume would re-grant the time budget",
        )
        return ResumeDecision(action="reject", reason="no persisted deadline_epoch in checkpoint")

    # ① 重建闭包对象:prepare/baseline 已被检查点跨过、不会重跑,而 ctx/baseline_commit
    #    按设计不进 state(不可序列化)——不重建,第一个续跑节点就会 assert ctx 判死
    baseline = str(values.get("baseline_commit") or "")
    baseline_from_head = False
    if not baseline:
        baseline = _workspace_head(run_dir / "workspace")
        baseline_from_head = bool(baseline)
    nodes.restore_runtime(values, baseline)
    nodes.deadline_epoch = persisted_deadline  # 原截止时刻原样带回,停机间隔计入

    # ② 被打断阶段的工作记忆(阶段与轮次都由 A 级读侧校验,不符即 None = 冷启动该阶段)
    loop_snapshot = None
    if stage:
        loop_snapshot = load_loop_snapshot(run_dir, stage=stage, round_no=round_no, task_id=task_id)
        nodes.resume_snapshot = loop_snapshot

    # ③ 按 next 节点分流(S03 表)
    if node in REVALIDATE_NODES:
        return _decide_revalidate(
            nodes,
            graph,
            run_dir=run_dir,
            config=config,
            values=values,
            node=node,
            round_no=round_no,
            checkpoint_id=checkpoint_id,
            remaining_limit=remaining_limit,
        )

    # 先记"从哪儿续的",再记"为此做了什么":轨迹顺序即决策顺序(M6 既有约定)
    _record_resume_from(
        nodes,
        checkpoint_id=checkpoint_id,
        node=node,
        stage=stage,
        round_no=round_no,
        values=values,
        loop_snapshot=loop_snapshot,
        baseline_from_head=baseline_from_head,
        action="continue",
    )
    reset_done = False
    reset_error = ""
    if node in PRE_RESET_NODES and baseline:
        try:
            reset_workspace(run_dir / "workspace", baseline)
            reset_done = True
        except Exception as exc:  # 复位失败绝不带着脏工作区续跑:那正是本规则要防的事
            log.exception("task %s: resume workspace reset failed", task_id)
            reset_error = f"{type(exc).__name__}: {exc}"
        nodes.tracker.record(
            tool="resume_reset_workspace",
            state="RESUME",
            input_payload={"node": node, "baseline": baseline[:12]},
            output_summary={"rolled_back": reset_done, "workspace": str(run_dir / "workspace")},
            error=reset_error or None,
        )
        if reset_error:
            return ResumeDecision(action="reject", reason=f"workspace reset failed: {reset_error}")

    resumed_config = dict(config)
    resumed_config["recursion_limit"] = remaining_limit
    return ResumeDecision(action="continue", config=resumed_config, detail={"node": node})


def _record_resume_from(
    nodes: TaskNodes,
    *,
    checkpoint_id: str,
    node: str,
    stage: str,
    round_no: int,
    values: dict[str, Any],
    loop_snapshot: Any,
    baseline_from_head: bool,
    action: str,
) -> None:
    nodes.tracker.record(
        tool="resume_from_checkpoint",
        round_no=round_no,
        state="RESUME",
        input_payload={
            "checkpoint_id": checkpoint_id,
            "next_node": node,
            "stage": stage or node,
            "resumed_status": values.get("status", ""),
            "action": action,
        },
        output_summary={
            "loop_snapshot_turns": loop_snapshot.turn_no if loop_snapshot else 0,
            "loop_snapshot_messages": len(loop_snapshot.messages) if loop_snapshot else 0,
            "tokens_used": int(values.get("tokens_used", 0) or 0),
            "turns": int(values.get("turns", 0) or 0),
            "baseline_from_head": baseline_from_head,
            "candidate_id": str(values.get("candidate_id", "") or "") or None,
        },
    )


def _decide_revalidate(
    nodes: TaskNodes,
    graph: Any,
    *,
    run_dir: Path,
    config: dict[str, Any],
    values: dict[str, Any],
    node: str,
    round_no: int,
    checkpoint_id: str,
    remaining_limit: int,
) -> ResumeDecision:
    """apply/verify/finish 的恢复:候选重应用 + 清旧结论 + 从 APPLY 完整重验。"""

    def _reject(reason: str) -> ResumeDecision:
        nodes.tracker.record(
            tool="resume_unavailable",
            round_no=round_no,
            state="RESUME",
            input_payload={"next_node": node, "reason": reason},
            error=f"resume rejected: {reason}",
        )
        return ResumeDecision(action="reject", reason=reason)

    candidate_id = str(values.get("candidate_id", "") or "")
    if not candidate_id:
        return _reject("checkpoint has no frozen candidate for a writable-stage resume")

    loaded = load_candidate(run_dir, candidate_id)
    if loaded is None:
        return _reject(f"candidate {candidate_id} missing or tampered")
    manifest, patch_text = loaded

    # 身份链核对:受理契约 → 候选清单 → 当前源内容 → 基线
    accepted_task_hash, accepted_source_hash = accepted_contract_hashes(run_dir)
    if not manifest.task_spec_hash or manifest.task_spec_hash != accepted_task_hash:
        return _reject("candidate task_spec_hash does not match accepted contract")
    if not manifest.source_snapshot_hash or manifest.source_snapshot_hash != accepted_source_hash:
        return _reject("candidate source_snapshot_hash does not match accepted contract")
    from app.task_spec import fingerprint_source_dir

    fresh_source = fingerprint_source_dir(nodes.bug.repo_dir)
    if fresh_source != manifest.source_snapshot_hash:
        return _reject("source changed since acceptance")
    baseline = str(values.get("baseline_commit") or "")
    if not baseline or manifest.baseline_commit != baseline:
        return _reject("candidate baseline mismatch")

    # 资源边界:无余额(任务级 token 已耗尽)不允许再烧重验的 pytest/模型调用
    budget = int(getattr(nodes.ledger, "token_limit", 0) or 0) if nodes.ledger else 0
    if budget > 0 and int(values.get("tokens_used", 0) or 0) >= budget:
        return _reject("no token budget left for revalidation")

    # 先记恢复决定,再做有副作用的动作(轨迹顺序即决策顺序)
    _record_resume_from(
        nodes,
        checkpoint_id=checkpoint_id,
        node=node,
        stage="",
        round_no=round_no,
        values=values,
        loop_snapshot=None,
        baseline_from_head=False,
        action="revalidate",
    )
    # 复位前先做干跑落点校验:被拒绝的恢复不得先毁掉崩溃现场
    check = check_patch_targets(run_dir / "workspace", patch_text)
    if check is not None and not check.applied:
        return _reject(f"candidate reapply would violate patch rules: {check.detail}")

    try:
        reapply_candidate(run_dir / "workspace", run_dir, candidate_id)
    except Exception as exc:
        return _reject(f"candidate reapply failed: {exc}")

    nodes.tracker.record(
        tool="resume_candidate_reapplied",
        round_no=round_no,
        state="RESUME",
        input_payload={"candidate_id": candidate_id, "next_node": node},
        output_summary={"baseline": manifest.baseline_commit[:12], "from_node": node},
    )

    # 清旧结论:verify/gate 布尔绝不跨恢复复用;state 以"propose 刚结束、
    # 候选已在工作区"的形态落检查点 → 下一个节点就是 apply(门禁/验证全量重跑)
    graph.update_state(
        config,
        {
            "status": "APPLY_PATCH",
            "verify_failed_ok": False,
            "verify_regression_ok": False,
            "gate_violations": [],
            "validation_status": "not_run",
            "gate_status": "not_run",
            "candidate_id": candidate_id,
            "candidate_hash": manifest.diff_sha256,
        },
        as_node="propose",
    )
    resumed_config = dict(config)
    resumed_config["recursion_limit"] = remaining_limit
    return ResumeDecision(
        action="revalidate",
        config=resumed_config,
        detail={"node": node, "candidate_id": candidate_id},
    )
