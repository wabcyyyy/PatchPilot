"""协作式取消注册表:task_id → threading.Event。

不强杀线程:cancel 方 set 事件,执行方在 turn 边界自查后抛 TaskCancelled。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class CancelRegistry:
    """按 task_id 注册/查询取消事件;线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._callbacks: dict[str, list[Callable[[], None]]] = {}

    def register(self, task_id: str) -> threading.Event:
        """注册任务(已注册则复用同一事件),返回取消事件。"""
        with self._lock:
            event = self._events.get(task_id)
            if event is None:
                event = threading.Event()
                self._events[task_id] = event
            return event

    def register_cleanup_callback(self, task_id: str, callback: Callable[[], None]) -> None:
        """为任务注册实时清理钩子(如 kill 正在运行的容器)。已取消则立即调用。"""
        with self._lock:
            event = self._events.get(task_id)
            if event is not None and event.is_set():
                try:
                    callback()
                except Exception as exc:
                    log.warning("cleanup callback for task %s failed: %s", task_id, exc)
                return
            self._callbacks.setdefault(task_id, []).append(callback)

    def unregister_cleanup_callback(self, task_id: str, callback: Callable[[], None]) -> None:
        """注销已完成阶段的清理钩子。"""
        with self._lock:
            cbs = self._callbacks.get(task_id)
            if cbs and callback in cbs:
                cbs.remove(callback)

    def request_cancel(self, task_id: str) -> bool:
        """请求取消;任务未注册(未在执行)返回 False。"""
        with self._lock:
            event = self._events.get(task_id)
            callbacks = list(self._callbacks.get(task_id, []))
        if event is None:
            return False
        event.set()
        for cb in callbacks:
            try:
                cb()
            except Exception as exc:
                log.warning("cleanup callback failed on task %s cancellation: %s", task_id, exc)
        return True

    def ids(self) -> list[str]:
        """当前注册的全部 task_id 快照(N-21 整改:停机时逐一传播取消)。"""
        with self._lock:
            return list(self._events)

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._events.pop(task_id, None)
            self._callbacks.pop(task_id, None)
