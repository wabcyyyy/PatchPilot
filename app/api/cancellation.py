"""协作式取消注册表:task_id → threading.Event。

不强杀线程:cancel 方 set 事件,执行方在 turn 边界自查后抛 TaskCancelled。
"""

from __future__ import annotations

import threading


class CancelRegistry:
    """按 task_id 注册/查询取消事件;线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def register(self, task_id: str) -> threading.Event:
        """注册任务(已注册则复用同一事件),返回取消事件。"""
        with self._lock:
            event = self._events.get(task_id)
            if event is None:
                event = threading.Event()
                self._events[task_id] = event
            return event

    def request_cancel(self, task_id: str) -> bool:
        """请求取消;任务未注册(未在执行)返回 False。"""
        with self._lock:
            event = self._events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._events.pop(task_id, None)
