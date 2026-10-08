"""崩溃恢复的 B 级接线:把 SqliteSaver 检查点读回可用的运行位置(M6)。

分工(两级恢复各管一段,缺一不可):
- **A 级**(`app/graph/loop_state.py`):Agent 循环的工作记忆(messages / token 累计 /
  轮次),因为 LangGraph 只在 superstep **之间**打检查点,一个阶段内部跑到第 19 轮崩溃时
  检查点里根本没有这 19 轮——没有 A 级,B 级只能把整个阶段冷启动重跑一遍。
- **B 级**(本模块 + `runner.run_task_graph(resume=True)`):图状态与"接下来该跑哪个节点",
  由 `graph.get_state()` 读回(检查点此前只写不读,是纯留档;本模块起它是权威位置)。

两条安全前提(架构决定,不可协商):
1. **工作区完整性**:被打断的阶段若能写文件(PROPOSE_PATCH 及其之后的任何节点),
   续跑前必须先把工作区复位到基线 commit。块补丁协议的锚点是拿**磁盘上的实际内容**
   校验的,崩溃残留的半截补丁会让续跑的补丁落到错误位置或静默错位——复位后由快照
   重放"模型已经说过要做的事",而不是重放"它当时改到一半的文件"。只读阶段
   (LOCALIZE/PLAN)不需要复位,复位反而会毁掉只读阶段留下的取证现场。
2. **门禁必须重跑**:恢复只带回升阶时的**工作记忆**,绝不带回任何**判断**。
   apply 门禁、verify 双测试集、E3 双跑一致性复核、最终 verdict 全部在续跑的那次执行里
   真实重算——崩溃前"某轮 verify 已过"这类结论不会因为存在检查点而被复用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.gitops.cmd import run_git
from app.gitops.rollback import reset_workspace
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
# 会写工作区的节点及其之后的节点:续跑前必须复位工作区(见模块 docstring 前提 1)
WRITABLE_OR_LATER_NODES: frozenset[str] = frozenset(
    {"propose", "apply", "verify", "rollback", "finish"}
)


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


def prepare_resume(
    nodes: TaskNodes,
    graph: Any,
    *,
    run_dir: Path,
    config: dict[str, Any],
    task_id: str,
) -> dict[str, Any] | None:
    """读检查点并重建运行时对象;返回续跑 config,None = 无可续位置(调用方冷启动)。

    调用侧用法(runner.run_task_graph(resume=True)):`config = prepare_resume(...)`,
    非 None 时 `graph.invoke(None, config)`——`None` 输入即"不带新状态、从检查点的
    `next` 节点继续",这是 LangGraph 官方的按 thread 续跑口径。
    """
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:
        # 无 checkpointer 的图会在这里报错:恢复能力依赖检查点,报不出来就冷启动
        log.warning("task %s: cannot read checkpoint state (%s)", task_id, exc)
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"reason": "get_state failed"},
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    values: dict[str, Any] = dict(snapshot.values or {})
    pending: tuple[str, ...] = tuple(snapshot.next or ())
    checkpoint_id = str((snapshot.config or {}).get("configurable", {}).get("checkpoint_id") or "")
    if not values or not pending:
        # values 空 = 该 thread 从无检查点;pending 空 = 图已到 END(崩溃前的收尾
        # 已落库),两种情况都没有"被打断的节点"可续
        log.warning(
            "task %s: nothing to resume (values=%s next=%s)", task_id, bool(values), pending
        )
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"has_values": bool(values), "next": list(pending)},
            error="no resumable position in checkpoint",
        )
        return None

    node = pending[0]
    stage = LOOP_NODE_STAGES.get(node, "")
    round_no = int(values.get("round_no", 1) or 1)

    # 递归额度先判,再动任何东西(顺序是安全要求,不是风格):
    # 一旦判定"无可续位置",调用方会退回既有终态口径,而那时崩溃现场必须还是崩溃时的样子
    # ——可写阶段的工作区复位会毁掉取证现场,不能为一个最终不续跑的决定先把它抹掉。
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
        return None

    # S02b(ADR-0009 §3):恢复只顺延原 deadline 的**剩余时间**,绝不重授。
    # 旧检查点没有持久化 deadline_epoch → 无可信旧截止时刻,恢复等于把 900s
    # 整段重发一遍——按规格降级 NEEDS_REVIEW,不猜。检查排在任何复位之前:
    # 被拒绝的恢复不得先毁掉崩溃现场。
    raw_deadline = values.get("deadline_epoch")
    try:
        persisted_deadline = float(raw_deadline) if raw_deadline is not None else None
    except (TypeError, ValueError):
        persisted_deadline = None
    if persisted_deadline is None:
        nodes.resume_rejected_reason = "no persisted deadline_epoch in checkpoint"
        nodes.tracker.record(
            tool="resume_unavailable",
            state="RESUME",
            input_payload={"next_node": node, "reason": "no persisted deadline_epoch"},
            error="checkpoint has no trusted deadline; resume would re-grant the time budget",
        )
        return None

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

    # 先记"从哪儿续的",再记"为此做了什么":轨迹顺序即决策顺序
    nodes.tracker.record(
        tool="resume_from_checkpoint",
        round_no=round_no,
        state="RESUME",
        input_payload={
            "checkpoint_id": checkpoint_id,
            "next_node": node,
            "stage": stage or node,
            "resumed_status": values.get("status", ""),
        },
        output_summary={
            "loop_snapshot_turns": loop_snapshot.turn_no if loop_snapshot else 0,
            "loop_snapshot_messages": len(loop_snapshot.messages) if loop_snapshot else 0,
            "tokens_used": int(values.get("tokens_used", 0) or 0),
            "turns": int(values.get("turns", 0) or 0),
            "baseline_from_head": baseline_from_head,
        },
    )

    # ③ 工作区完整性(模块 docstring 前提 1):可写阶段一律先复位到基线
    reset_done = False
    reset_error = ""
    if node in WRITABLE_OR_LATER_NODES and baseline:
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
            return None
    log.info(
        "task %s: resuming from checkpoint %s at node %s (snapshot=%s, reset=%s)",
        task_id,
        checkpoint_id[:12],
        node,
        bool(loop_snapshot),
        reset_done,
    )

    # 只顺延"剩余额度",不额外放宽(AGENTS.md:不得借恢复之名放松资源界):
    # LangGraph 的 stop = 检查点当前 step + recursion_limit,传 original - 已烧步数后,
    # 整条 thread 的累计步数上界仍是原来那次运行的 5N+8——崩溃不该换来更多的循环余地。
    # (额度是否早已烧完,已在函数开头复位之前就判过并 return None。)
    resumed_config = dict(config)
    resumed_config["recursion_limit"] = remaining_limit
    return resumed_config
