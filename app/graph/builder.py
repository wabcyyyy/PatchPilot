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
    graph.add_conditional_edges(
        "localize", nodes.route_localize, {"continue": "propose", "end": END}
    )
    graph.add_conditional_edges("propose", nodes.route_propose, {"apply": "apply", "end": END})
    graph.add_conditional_edges(
        "apply",
        nodes.route_apply,
        {"verify": "verify", "retry": "propose", "exhausted": "rollback"},
    )
    graph.add_conditional_edges(
        "verify", nodes.route_verify, {"finish": "finish", "rollback": "rollback"}
    )
    graph.add_edge("finish", END)
    graph.add_conditional_edges(
        "rollback", nodes.route_rollback, {"propose": "propose", "end": END}
    )

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()
