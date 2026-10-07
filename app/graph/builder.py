"""LangGraph 图构建:状态、节点与条件边(企划书 4.2 转移表)。"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import TaskNodes
from app.graph.state import TaskState


def build_graph(nodes: TaskNodes, checkpointer: Any | None = None) -> Any:
    """按转移表装配图;checkpointer 提供时编译为可恢复图。"""
    graph = StateGraph(TaskState)

    graph.add_node("prepare", nodes.prepare)
    graph.add_node("baseline", nodes.baseline)
    graph.add_node("localize", nodes.localize)
    graph.add_node("plan", nodes.plan)
    graph.add_node("propose", nodes.propose)
    graph.add_node("apply", nodes.apply)
    graph.add_node("verify", nodes.verify)
    graph.add_node("finish", nodes.finish)
    graph.add_node("rollback", nodes.rollback)

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", nodes.route_prepare, {"continue": "baseline", "end": END}
    )
    graph.add_conditional_edges(
        "baseline", nodes.route_baseline, {"continue": "localize", "end": END}
    )
    # M5:范式是 LOCALIZE → PLAN → ACT(propose/apply)→ VERIFY,故 localize 之后先进 plan
    graph.add_conditional_edges("localize", nodes.route_localize, {"continue": "plan", "end": END})
    graph.add_conditional_edges("plan", nodes.route_plan, {"continue": "propose", "end": END})
    graph.add_conditional_edges("propose", nodes.route_propose, {"apply": "apply", "end": END})
    graph.add_conditional_edges(
        "apply",
        nodes.route_apply,
        # M5:门禁拒绝的重试边指向 plan——被拒的那一轮要**先重规划**再动手,
        # 而不是带着同一份旧计划把同一个补丁形状再赌一次
        {"verify": "verify", "retry": "plan", "exhausted": "rollback"},
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
