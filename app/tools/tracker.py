"""轨迹记录器:每个工具调用一条 JSONL 事件(企划书第 5 节格式)。

先落文件(runs/<task>/trajectory.jsonl),入库在 storage 层;文件副本永久保留。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
    """线程安全地追加轨迹事件(内存 + JSONL 落盘)。"""

    def __init__(self, path: Path | None, task_id: str = "") -> None:
        self.path = path
        self.task_id = task_id
        self.events: list[TrajectoryEvent] = []
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
        with self._lock:
            self.events.append(event)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
        log.debug("trajectory: %s round=%s err=%s", tool, round_no, error)
        return event.request_id
