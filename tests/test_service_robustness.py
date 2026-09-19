"""任务创建路径的健壮性:落库/调度失败必须释放锁并收敛任务状态(评审整改)。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.api.service import TaskService
from app.errors import PatchPilotError
from app.storage.repository import Repository


def _service(tmp_path: Path) -> TaskService:
    return TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
    )


def _idem(bug_id: str, engine: str, model: str) -> str:
    return hashlib.sha256(f"{bug_id}|{engine}|{model}".encode()).hexdigest()


def test_db_failure_releases_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """落库失败:异常上抛,但锁必须释放(否则同键任务被卡到 TTL)。"""
    service = _service(tmp_path)

    def _disk_full(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service.repo, "create_task", _disk_full)
    with pytest.raises(OSError, match="disk full"):
        service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    assert service.lock.acquire(f"task:{_idem('BUG-001', 'graph', 'fake')}", ttl_seconds=5)
    service.shutdown()


def test_submit_after_shutdown_converges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """线程池已关停(服务退出竞态):409 收敛,任务行标 NEEDS_REVIEW,锁释放。"""
    service = _service(tmp_path)

    def _shut_down(*args, **kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(service._pool, "submit", _shut_down)
    with pytest.raises(PatchPilotError, match="shutting down"):
        service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    tasks = service.repo.list_tasks(10)
    assert any(t["status"] == "NEEDS_REVIEW" for t in tasks)
    assert service.lock.acquire(f"task:{_idem('BUG-001', 'graph', 'fake')}", ttl_seconds=5)
    service.shutdown()


def test_create_task_wires_max_rounds_into_engine(tmp_path: Path) -> None:
    """N-9 整改:max_rounds 此前只落库不生效;引擎读 bug.max_rounds,必须在提交前覆写。"""
    service = _service(tmp_path)
    captured: dict[str, object] = {}

    def _capture(task_id, bug, *args, **kwargs):
        captured["max_rounds"] = bug.max_rounds

    service._execute = _capture  # type: ignore[method-assign]
    service.create_task(bug_id="BUG-001", engine="graph", model="fake", max_rounds=1)
    assert captured["max_rounds"] == 1
    service.shutdown()
