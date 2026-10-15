"""有界受理与准入控制 (P10-1)。

契约与边界:
1. 在源码 hash/复制/快照冻结或预检之前取得有限 admission permit;
2. 并发受理与排队分别限额，满队列或超时返回明确错误码与 Retry-After;
3. 全局紧急停发 (emergency_stop) 立即拒绝所有新任务受理。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from app.errors import PatchPilotError

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)


class AdmissionError(PatchPilotError):
    """准入错误基类。"""


class EmergencyStopError(AdmissionError):
    """全局紧急停发，暂停新任务受理。"""


class AdmissionQueueFullError(AdmissionError):
    """受理排队队列已满。"""


class AdmissionTimeoutError(AdmissionError):
    """等待受理许可超时。"""


class AdmissionController:
    """准入与受理并发控制器。"""

    def __init__(
        self,
        concurrency_limit: int = 2,
        queue_limit: int = 20,
        timeout_seconds: float = 5.0,
        emergency_stop: bool = False,
    ) -> None:
        self.concurrency_limit = max(1, concurrency_limit)
        self.queue_limit = max(0, queue_limit)
        self.timeout_seconds = max(0.1, timeout_seconds)
        self._emergency_stop = emergency_stop

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_count = 0
        self._waiting_count = 0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AdmissionController:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        return cls(
            concurrency_limit=settings.admission_concurrency_limit,
            queue_limit=settings.admission_queue_limit,
            timeout_seconds=settings.admission_timeout_seconds,
            emergency_stop=settings.emergency_stop,
        )

    def set_emergency_stop(self, active: bool) -> None:
        """设置全局紧急停发状态。"""
        with self._lock:
            self._emergency_stop = active
            if active:
                # 唤醒所有排队者以便立刻被拒绝
                self._condition.notify_all()
        log.warning("emergency_stop set to %s", active)

    @property
    def is_emergency_stop(self) -> bool:
        with self._lock:
            return self._emergency_stop

    @contextlib.contextmanager
    def acquire_permit(self) -> Iterator[None]:
        """获取任务受理许可的上下文管理器。"""
        with self._condition:
            if self._emergency_stop:
                raise EmergencyStopError("emergency stop is active; task admission suspended")

            if self._active_count >= self.concurrency_limit:
                if self._waiting_count >= self.queue_limit:
                    raise AdmissionQueueFullError(
                        f"admission queue is full ({self._waiting_count} >= {self.queue_limit}); retry later"
                    )

                self._waiting_count += 1
                deadline = time.monotonic() + self.timeout_seconds
                try:
                    while self._active_count >= self.concurrency_limit:
                        if self._emergency_stop:
                            raise EmergencyStopError(
                                "emergency stop is active; task admission suspended"
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AdmissionTimeoutError(
                                f"admission permit acquire timed out after {self.timeout_seconds}s"
                            )
                        self._condition.wait(timeout=remaining)
                finally:
                    self._waiting_count -= 1

            self._active_count += 1

        try:
            yield
        finally:
            with self._condition:
                self._active_count -= 1
                self._condition.notify()

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "active_admissions": self._active_count,
                "waiting_admissions": self._waiting_count,
                "concurrency_limit": self.concurrency_limit,
                "queue_limit": self.queue_limit,
                "emergency_stop": self._emergency_stop,
            }


_GLOBAL_ADMISSION_CONTROLLER: AdmissionController | None = None
_ADMISSION_LOCK = threading.Lock()


def get_admission_controller(settings: Settings | None = None) -> AdmissionController:
    global _GLOBAL_ADMISSION_CONTROLLER
    with _ADMISSION_LOCK:
        if _GLOBAL_ADMISSION_CONTROLLER is None:
            _GLOBAL_ADMISSION_CONTROLLER = AdmissionController.from_settings(settings)
        return _GLOBAL_ADMISSION_CONTROLLER


def reset_admission_controller(settings: Settings | None = None) -> AdmissionController:
    global _GLOBAL_ADMISSION_CONTROLLER
    with _ADMISSION_LOCK:
        _GLOBAL_ADMISSION_CONTROLLER = AdmissionController.from_settings(settings)
        return _GLOBAL_ADMISSION_CONTROLLER
