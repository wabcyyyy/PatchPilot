"""LangGraph 图构建:状态、节点与条件边(企划书 4.2 转移表)。"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import TaskNodes
from app.graph.state import TaskState


def build_graph(nodes: TaskNodes, checkpointer: Any | None = None) -> Any:
    """按转移表装配图;checkpointer 提供时编译为可恢复图。"""
    graph = StateGraph(TaskState)

    def traced(
        action: Callable[[TaskState], dict[str, Any]],
    ) -> Callable[[TaskState], dict[str, Any]]:
        @wraps(action)
        def run(state: TaskState) -> dict[str, Any]:
            started = time.monotonic()
            update: dict[str, Any] = {}
            error: str | None = None
            try:
                update = action(state)
                memory = getattr(nodes, "memory", None)
                if memory is not None:
                    update["memory_version"] = memory.version
                    update["memory_evidence_refs"] = [item.ref for item in memory.evidence]
                return update
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                nodes.tracker.record(
                    tool="node_transition",
                    round_no=state.get("round_no", 0),
                    state=action.__name__.upper(),
                    input_payload={"node": action.__name__, "status": state.get("status")},
                    output_summary={
                        "status": update.get("status"),
                        "round_no": update.get("round_no", state.get("round_no", 0)),
                    },
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=error,
                )

        return run

    graph.add_node("prepare", traced(nodes.prepare))
    graph.add_node("baseline", traced(nodes.baseline))
    graph.add_node("localize", traced(nodes.localize))
    # G10:issue-only 任务的验收清单节点(仅 diagnose 模式经过;repair 任务的
    # 条件边不指向它,超步数与引入前逐字相同)
    graph.add_node("diagnose", traced(nodes.diagnose))
    graph.add_node("plan", traced(nodes.plan))
    graph.add_node("propose", traced(nodes.propose))
    graph.add_node("apply", traced(nodes.apply))
    graph.add_node("verify", traced(nodes.verify))
    graph.add_node("finish", traced(nodes.finish))
    graph.add_node("rollback", traced(nodes.rollback))

    graph.add_edge(START, "prepare")
    # G10:diagnose 模式 prepare 后直接进调查(baseline 节点对空集等价于全仓跑)
    graph.add_conditional_edges(
        "prepare",
        nodes.route_prepare,
        {"continue": "baseline", "localize": "localize", "end": END},
    )
    graph.add_conditional_edges(
        "baseline", nodes.route_baseline, {"continue": "localize", "end": END}
    )
    # M5:范式是 LOCALIZE → PLAN → ACT(propose/apply)→ VERIFY,故 localize 之后先进 plan
    # G10:diagnose 模式在 localize 之后插入验收清单节点(diagnose → plan/END)
    graph.add_conditional_edges(
        "localize",
        nodes.route_localize,
        {"continue": "plan", "diagnose": "diagnose", "end": END},
    )
    graph.add_conditional_edges("diagnose", nodes.route_diagnose, {"continue": "plan", "end": END})
    graph.add_conditional_edges("plan", nodes.route_plan, {"continue": "propose", "end": END})
    graph.add_conditional_edges("propose", nodes.route_propose, {"apply": "apply", "end": END})
    graph.add_conditional_edges(
        "apply",
        nodes.route_apply,
        # S04/F5:门禁拒绝统一进 rollback(保全→复位→至多一次递增→重试回 plan/
        # 耗尽终止);M5 的"先重规划"语义由 rollback→plan 这条既有边保持——
        # 被拒的那一轮仍要先修订计划再动手,只是递增不再发生在 apply 里
        {"verify": "verify", "rollback": "rollback"},
    )
    graph.add_conditional_edges(
        "verify",
        nodes.route_verify,
        # "end": E3 double-run 不一致 → NEEDS_REVIEW 终点(与 localize 的 end 同构)
        {"finish": "finish", "rollback": "rollback", "end": END},
    )
    graph.add_edge("finish", END)
    graph.add_conditional_edges(
        "rollback",
        nodes.route_rollback,
        # "propose":这个**键名**沿用 route_rollback 的返回串(被 tests/test_branching.py 钉住),
        # 但 M5 把它的**节点目标**换成 plan:验证失败回滚后,下一轮先带反馈修订计划,
        # 再由 propose 按计划动手(状态串 PROPOSE_PATCH 不变,终态语义不变)。
        # "apply":卡5b 自适应分支的胜者补丁合流后,交回既有 apply 节点做图级门禁复核
        # (门禁链照常全量执行,不为分支开旁路)
        {"propose": "plan", "end": END, "apply": "apply"},
    )

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
