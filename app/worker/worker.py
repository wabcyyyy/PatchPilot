"""独立 Worker 进程主循环 (P11-2)。

职责：
- 进程级单实例保护 (worker.lock)，防止多个 worker 并发踩踏同一数据源；
- 轮询持久作业队列 (jobs)，按优先级与 FIFO 领取待执行作业；
- 任务执行期间在后台守护线程持续续期租约 (renew_job_lease 心跳)；
- 驱动 TaskService 完成任务执行、工件持久化、终态翻写与配额结算；
- 优雅停机响应 (stop 信号)，安全退出。
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid
from pathlib import Path
from typing import Any

from app.api.service import TaskService
from app.config import get_settings
from app.storage.locks import SingleInstanceLock
from app.storage.repository import Repository

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        repo: Repository,
        service: TaskService | None = None,
        runs_root: Path | str | None = None,
        worker_id: str | None = None,
        lease_duration_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        lock_path: Path | str | None = None,
    ) -> None:
        settings = get_settings()
        self.repo = repo
        runs_dir = Path(runs_root) if runs_root else settings.runs_root
        self.service = service or TaskService(repo=repo, runs_root=runs_dir)
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_duration = (
            lease_duration_seconds
            if lease_duration_seconds is not None
            else settings.job_lease_duration_seconds
        )
        self.poll_interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.job_poll_interval_seconds
        )
        resolved_lock = lock_path or (settings.runs_root / "worker.lock")
        self.instance_lock = SingleInstanceLock(resolved_lock)
        self._stop_event = threading.Event()
        self._current_job: dict[str, Any] | None = None

    def run_once(self) -> bool:
        """尝试从持久队列中 claim 一个 job 并执行。

        如果有 job 执行返回 True，如果队列为空返回 False。
        """
        job = self.repo.claim_next_job(
            worker_id=self.worker_id,
            lease_duration_seconds=self.lease_duration,
        )
        if not job:
            return False

        task_id = job["task_id"]
        fencing_token = int(job.get("fencing_token", 0))
        self._current_job = job
        log.info(
            "worker %s claimed job %s for task %s (token=%d)",
            self.worker_id,
            job["id"],
            task_id,
            fencing_token,
        )

        cancel_event = threading.Event()
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, fencing_token, stop_heartbeat, cancel_event),
            daemon=True,
            name=f"heartbeat-{task_id[:8]}",
        )
        heartbeat.start()
        try:
            self.service.execute_task_from_job(
                task_id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                cancel_event=cancel_event,
            )
        except Exception:
            log.exception(
                "worker %s encountered execution error for task %s",
                self.worker_id,
                task_id,
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2.0)
            self._current_job = None
        return True

    def _heartbeat_loop(
        self,
        task_id: str,
        fencing_token: int,
        stop_event: threading.Event,
        cancel_event: threading.Event,
    ) -> None:
        interval = max(self.lease_duration / 3.0, 0.5)
        while not stop_event.wait(interval):
            if self._stop_event.is_set():
                break
            try:
                renewed = self.repo.renew_job_lease(
                    task_id=task_id,
                    worker_id=self.worker_id,
                    lease_duration_seconds=self.lease_duration,
                    fencing_token=fencing_token,
                )
                if not renewed:
                    log.warning(
                        "worker %s lost lease for task %s (token=%d); stopping heartbeat and cancelling execution",
                        self.worker_id,
                        task_id,
                        fencing_token,
                    )
                    cancel_event.set()
                    break
            except Exception:
                log.exception("worker %s heartbeat error for task %s", self.worker_id, task_id)

    def start(self) -> None:
        """主循环：不断轮询作业队列执行任务，直到被 stop。"""
        with self.instance_lock:
            self._stop_event.clear()
            log.info(
                "worker %s started (lease=%.1fs, poll=%.1fs)",
                self.worker_id,
                self.lease_duration,
                self.poll_interval,
            )
            while not self._stop_event.is_set():
                try:
                    ran = self.run_once()
                    if not ran:
                        self._stop_event.wait(self.poll_interval)
                except Exception:
                    log.exception("worker %s loop error", self.worker_id)
                    self._stop_event.wait(self.poll_interval)
            log.info("worker %s stopped gracefully", self.worker_id)

    def stop(self) -> None:
        """优雅停止 worker。"""
        log.info("stopping worker %s...", self.worker_id)
        self._stop_event.set()


def run_worker() -> None:
    """Worker 进程 CLI 入口。"""
    settings = get_settings()
    repo = Repository(settings.db_path)
    service = TaskService(repo=repo, runs_root=settings.runs_root)
    worker = Worker(repo=repo, service=service)

    def _on_signal(sig, frame):
        worker.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    worker.start()


if __name__ == "__main__":
    run_worker()
