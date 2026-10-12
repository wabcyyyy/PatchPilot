"""任务事件流(G12):`GET /api/tasks/{id}/events` 的 SSE 事实层。

设计边界(SPEC §7 G12):

- **唯一事实来源是轨迹**(`trajectory_events` 表)。SSE 不另记一份进度、不自己算
  阶段——它把与 `GET /tasks/{id}/trajectory` 相同的事件行换成可读类型后推送,
  两条消费面永远看到同一组事实(CLI/API 不会各自讲一套话);
- 显示面只做**类型归类与去噪**:阶段、tool_started/tool_finished、候选、等待、
  usage、终态。provider 思维链从不进轨迹(T24),这里也不会凭空多出来;
- 续传语义:`id:` 取事件行的自增 id,客户端凭 Last-Event-ID(或查询参数)只收后面的
  事件;断连重连不重复、不丢(重连时先补发未读事件再续推);
- 终态帧是流的正常结束:任务行已终态时先冲掉全部未读事件,再发一条
  `task_status`(terminal=true)然后关流,不把客户端挂在一条永不完结的连接上。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from app.tools.registry import REGISTRY

if TYPE_CHECKING:  # 只为类型标注:运行时避免把服务层拖进导入环
    from app.api.service import TaskService

# 事件类型(客户端按它分流渲染,而不是读 tool 名猜):
TYPE_TOOL_STARTED = "tool_started"
TYPE_TOOL_FINISHED = "tool_finished"
TYPE_USAGE = "usage"
TYPE_WAITING = "waiting"
TYPE_CANDIDATE = "candidate"
TYPE_PROGRESS = "progress"
TYPE_TASK_STATUS = "task_status"

#: 直接"等人在场"的事件——它们出现时任务行通常正处在 WAITING_INPUT
_WAITING_TOOLS = frozenset({"acceptance_waiting", "clarification_required"})
_CANDIDATE_TOOLS = frozenset({"candidate_frozen", "resume_candidate_reapplied"})
#: 真正的工具执行事件(注册表内的名字 + finish 伪工具);其余都算平台进度
_EXECUTION_TOOLS = frozenset({*REGISTRY.keys()})


def classify_event(tool: str) -> str:
    """轨迹事件 → 显示类型:让 CLI/API 按类型渲染,而不是按工具名各自猜。"""
    if tool == "tool_started":
        return TYPE_TOOL_STARTED
    if tool == "llm":
        return TYPE_USAGE
    if tool in _WAITING_TOOLS:
        return TYPE_WAITING
    if tool in _CANDIDATE_TOOLS:
        return TYPE_CANDIDATE
    if tool in _EXECUTION_TOOLS:
        return TYPE_TOOL_FINISHED
    return TYPE_PROGRESS


def sse_frame(*, event_id: int | None, event_type: str, payload: dict[str, Any]) -> str:
    """一条 SSE 帧:可选 `id:`(事件行号,续传锚点)+ `event:` + 单行 JSON data。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False, default=str)}")
    return "\n".join(lines) + "\n\n"


def event_frame(row: dict[str, Any]) -> str:
    """轨迹行 → SSE 帧(与 trajectory API 同一组事实,另附类型字段)。"""
    tool = str(row.get("tool") or "")
    payload = {
        "event_id": row.get("event_id"),
        "type": classify_event(tool),
        "stage": row.get("state"),
        "tool": tool,
        "round": row.get("round"),
        "timestamp": row.get("timestamp"),
        "duration_ms": row.get("duration_ms"),
        "error": row.get("error"),
        "input": row.get("input"),
        "output_summary": row.get("output_summary"),
    }
    row_id = row.get("id")
    return sse_frame(
        event_id=int(row_id) if isinstance(row_id, int) else None,
        event_type=str(payload["type"]),
        payload=payload,
    )


def status_frame(task: dict[str, Any], *, terminal: bool) -> str:
    """任务状态帧(无 id:重连时按当前状态重发,不参与 Last-Event-ID)。"""
    payload = {
        "type": TYPE_TASK_STATUS,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "verdict": task.get("verdict"),
        "stage": task.get("stage"),
        "terminal": terminal,
    }
    return sse_frame(event_id=None, event_type=TYPE_TASK_STATUS, payload=payload)


def stream_task_events(
    service: TaskService,
    task_id: str,
    *,
    after_id: int = 0,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
    max_polls: int = 0,
) -> Iterator[str]:
    """按行产出 SSE 帧;任务到终态并冲净事件后结束(max_polls>0 仅供离线测试)。

    开场只在**没有未读事件**时发一条现场状态帧(重连到已完结任务不会挂住);
    有未读事件时先冲事件、最后以 terminal 状态帧收口——客户端永远不会在看到终态帧之后
    才发现还有没收到的事件。
    """
    from app.storage.repository import TERMINAL_STATUSES

    last_id = max(int(after_id or 0), 0)
    task = service.get_task(task_id) or {}
    terminal = str(task.get("status") or "") in TERMINAL_STATUSES
    has_backlog = bool(service.events_since(task_id, last_id, limit=1))
    if terminal and not has_backlog:
        # 终态且没有未读事件:一条状态帧就是全部(重连到已完结任务立即收口)
        yield status_frame(task, terminal=True)
        return
    if not has_backlog:
        yield status_frame(task, terminal=terminal)

    last_emit = time.monotonic()
    polls = 0
    while True:
        rows = service.events_since(task_id, last_id, limit=200)
        for row in rows:
            row_id = row.get("id")
            if isinstance(row_id, int):
                last_id = max(last_id, row_id)
            yield event_frame(row)
            last_emit = time.monotonic()
        task = service.get_task(task_id) or {}
        terminal = str(task.get("status") or "") in TERMINAL_STATUSES
        if terminal and not rows:
            yield status_frame(task, terminal=True)
            return
        if heartbeat_seconds > 0 and time.monotonic() - last_emit >= heartbeat_seconds:
            yield ": ping\n\n"
            last_emit = time.monotonic()
        polls += 1
        if max_polls and polls >= max_polls:
            yield status_frame(task, terminal=terminal)
            return
        time.sleep(max(poll_seconds, 0.01))
