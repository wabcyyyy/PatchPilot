"""最小等待/批准契约(G10;G11 在同一机制上扩展任意澄清,不建第二套暂停系统)。

- `ApprovalRegistry` 是服务层持有的一份"任务 → 批准事件"登记:执行线程等待前登记,
  批准端点经 `approve` 唤醒同进程内的等待线程;进程已死(无 waiter)时调用方走
  重新入队路径,不假装有人被唤醒。
- `ControlEvent` 是控制事实的载体:事件 id、来源、内容 hash、expected_revision、
  幂等键。`source` 只能由平台写;模型文字永远不构成 trusted_user 批准(§3.2 的
  低信任输入原则在这里落成类型)。
- `wait_for_approval` 是唯一的等待语义:0 = 不等待(headless),超时返回显式结论,
  不无限占用执行线程。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SOURCE_PLATFORM = "platform"
SOURCE_TRUSTED_USER = "trusted_user"

KIND_ACCEPTANCE_REQUEST = "acceptance_request"

WAIT_APPROVED = "approved"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ControlEvent:
    """一条控制事实:由平台写,模型不能伪造来源。"""

    event_id: str
    task_id: str
    kind: str
    source: str
    payload_hash: str
    expected_revision: int = 0
    question_id: str = ""  # G11 澄清问题的引用位;验收批准留空
    idem_key: str = ""  # 同一问题的重复提交经它幂等
    created_at: str = ""

    @staticmethod
    def create(
        *,
        task_id: str,
        kind: str,
        source: str,
        payload_hash: str,
        expected_revision: int = 0,
        question_id: str = "",
        idem_key: str = "",
    ) -> ControlEvent:
        if source not in (SOURCE_PLATFORM, SOURCE_TRUSTED_USER):
            raise ValueError(f"unsupported control event source: {source!r}")
        return ControlEvent(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            kind=kind,
            source=source,
            payload_hash=payload_hash,
            expected_revision=expected_revision,
            question_id=question_id,
            idem_key=idem_key,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "source": self.source,
            "payload_hash": self.payload_hash,
            "expected_revision": self.expected_revision,
            "question_id": self.question_id,
            "idem_key": self.idem_key,
            "created_at": self.created_at,
        }


class ApprovalRegistry:
    """任务级批准事件登记:同进程内唤醒等待线程的唯一通道。线程安全。"""

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str) -> threading.Event:
        with self._lock:
            event = self._events.get(task_id)
            if event is None:
                event = threading.Event()
                self._events[task_id] = event
            return event

    def approve(self, task_id: str) -> bool:
        """唤醒等待线程;返回 False = 本进程没有该任务的 waiter(调用方走重排队)。"""
        with self._lock:
            event = self._events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def has_waiter(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._events

    def release(self, task_id: str) -> None:
        with self._lock:
            self._events.pop(task_id, None)


def wait_for_approval(
    event: threading.Event | None,
    wait_seconds: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """等待批准:0 = 不等待(headless);超时返回 WAIT_TIMEOUT,不无限占用线程。

    取消经 `cancel_event` 协作式中断(默认 None = 不监听,与引入取消之前逐字相同):
    等待窗可能长达 acceptance_wait_seconds(默认 3600s),不能把取消拖到等待结束——
    每 0.25s 醒一次查取消;取消优先于慢到的批准(批准已经落盘,取消只是停动作,
    清单仍在盘上供人工复核)。
    """
    if event is None:
        return WAIT_TIMEOUT
    if wait_seconds <= 0:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        return WAIT_APPROVED if event.is_set() else WAIT_TIMEOUT
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WAIT_TIMEOUT
        if event.wait(timeout=min(0.25, remaining)):
            return WAIT_APPROVED
