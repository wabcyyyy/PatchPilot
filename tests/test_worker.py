"""P11-2 & P11-3 测试: 独立 Worker 循环、单实例保护与部署隔离 (Worker & SingleInstanceLock)。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import TaskService
from app.config import get_settings
from app.errors import SingleInstanceError
from app.storage.locks import SingleInstanceLock
from app.storage.repository import Repository
from app.worker.worker import Worker


def test_single_instance_lock(tmp_path: Path) -> None:
    lock_file = tmp_path / "test.lock"
    lock1 = SingleInstanceLock(lock_file)
    lock2 = SingleInstanceLock(lock_file)

    assert lock1.acquire() is True
    assert lock2.acquire() is False

    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


def test_single_instance_lock_context_manager(tmp_path: Path) -> None:
    lock_file = tmp_path / "test_ctx.lock"
    with (
        SingleInstanceLock(lock_file),
        pytest.raises(SingleInstanceError, match="another instance is already running"),
        SingleInstanceLock(lock_file),
    ):
        pass


def test_worker_run_once_no_jobs(tmp_path: Path) -> None:
    db_file = tmp_path / "empty_worker.db"
    repo = Repository(db_file)
    worker = Worker(repo=repo, runs_root=tmp_path / "runs", lock_path=tmp_path / "worker.lock")
    assert worker.run_once() is False


def test_worker_run_once_executes_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "exec_worker.db"
    runs_dir = tmp_path / "runs"
    repo = Repository(db_file)

    # 禁用 API 进程内 worker 执行，仅入队
    monkeypatch.setenv("PATCHPILOT_WORKER_ENABLED", "false")
    get_settings.cache_clear()

    service = TaskService(repo=repo, runs_root=runs_dir)
    worker = Worker(
        repo=repo,
        service=service,
        runs_root=runs_dir,
        lock_path=tmp_path / "worker.lock",
        worker_id="test-worker-1",
    )

    task, is_new = service.create_task(
        bug_id="BUG-003",
        engine="plain",
        model="fake",
        principal_id="test-princ",
    )
    assert is_new is True
    task_id = task["id"]

    # 验证 job 在队列中处于 pending
    job_before = repo.get_job_by_task_id(task_id)
    assert job_before is not None
    assert job_before["status"] == "pending"

    # Worker 执行一次
    ran = worker.run_once()
    assert ran is True

    # 验证 job 已完成且租约被清空
    job_after = repo.get_job_by_task_id(task_id)
    assert job_after is not None
    assert job_after["status"] == "completed"
    assert job_after["lease_expires_at"] is None

    # 验证 task 状态已完成
    finished_task = repo.get_task(task_id)
    assert finished_task is not None
    assert finished_task["status"] == "FINISHED"
    assert finished_task["verdict"] == "resolved"


def test_worker_start_and_stop_lifecycle(tmp_path: Path) -> None:
    db_file = tmp_path / "loop_worker.db"
    repo = Repository(db_file)
    worker = Worker(
        repo=repo,
        poll_interval_seconds=0.05,
        lock_path=tmp_path / "worker.lock",
    )

    t = threading.Thread(target=worker.start, daemon=True)
    t.start()
    time.sleep(0.15)
    assert t.is_alive()

    worker.stop()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_api_production_single_instance_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "runs"
    db_file = tmp_path / "prod.sqlite3"
    monkeypatch.setenv("PATCHPILOT_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("PATCHPILOT_API_TOKEN", "test-prod-secret-token")
    monkeypatch.setenv("PATCHPILOT_RUNS_ROOT", str(runs_dir))
    monkeypatch.setenv("PATCHPILOT_DB_PATH", str(db_file))
    monkeypatch.setenv("PATCHPILOT_MODEL_EGRESS_MODE", "disabled")
    get_settings.cache_clear()

    app1 = create_app(db_path=db_file, runs_root=runs_dir)
    app2 = create_app(db_path=db_file, runs_root=runs_dir)

    with (
        TestClient(app1),
        pytest.raises(SingleInstanceError, match="another instance is already running"),
        TestClient(app2),
    ):
        pass
