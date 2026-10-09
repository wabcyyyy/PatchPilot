"""轨迹记录器:每个工具调用一条 JSONL 事件(企划书第 5 节格式)。

先落文件(runs/<task>/trajectory.jsonl),入库在 storage 层。
保留承诺(P3-12 整改,如实版):轨迹属**取证集,永久保留**;
工作区副本等**可弃集**由终态回收器回收(app/api/recycle.py)——
「全部永久」与无界增长在数学上不可同时成立(审计 R3-Q3)。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class TrajectoryEvent:
    task_id: str
    round: int
    state: str
    tool: str
    request_id: str
    input: dict[str, object] = field(default_factory=dict)
    output_summary: object = None
    duration_ms: int = 0
    error: str | None = None
    timestamp: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.request_id,
            "task_id": self.task_id,
            "round": self.round,
            "state": self.state,
            "tool": self.tool,
            "request_id": self.request_id,
            "input": self.input,
            "output_summary": self.output_summary,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class Tracker:
    """线程安全地追加轨迹事件(内存 + JSONL 落盘)。

    S07:可选 sink——每条事件在 **JSONL 落盘之后**(JSONL 是原始取证来源与
    真相层)在 Tracker 锁**之外**调用(service 把事件幂等写入 SQLite 并推进
    进度列)。sink 抛错绝不打断运行:计数到 sink_failures,由收尾
    insert_events 补录;失败数即"实时入库降级"的可观测证据。
    """

    def __init__(
        self,
        path: Path | None,
        task_id: str = "",
        *,
        sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.events: list[TrajectoryEvent] = []
        self.sink = sink
        self.sink_failures = 0
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        tool: str,
        round_no: int = 0,
        state: str = "-",
        input_payload: dict[str, object] | None = None,
        output_summary: object = None,
        duration_ms: int = 0,
        error: str | None = None,
    ) -> str:
        event = TrajectoryEvent(
            task_id=self.task_id,
            round=round_no,
            state=state,
            tool=tool,
            request_id=uuid.uuid4().hex,
            input=input_payload or {},
            output_summary=output_summary,
            duration_ms=duration_ms,
            error=error,
            timestamp=_now_iso(),
        )
        payload = event.as_dict()
        with self._lock:
            self.events.append(event)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if self.sink is not None:
            try:
                self.sink(payload)  # 锁外执行:SQLite 写不能持 Tracker 锁(锁序倒置)
            except Exception as exc:  # 实时入库降级可观测;收尾补录兜底
                self.sink_failures += 1
                log.warning(
                    "trajectory sink failed (%s failures, tool=%s): %s",
                    self.sink_failures,
                    tool,
                    exc,
                )
        log.debug("trajectory: %s round=%s err=%s", tool, round_no, error)
        return event.request_id
