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


def test_recover_stale_releases_stale_locks(tmp_path: Path) -> None:
    """N-20 整改:启动恢复必须同步清掉残留任务锁——否则崩溃重启后
    同键重试会被 409 卡死到 TTL(约 16 分钟),且报错语义错误。"""

    service = _service(tmp_path)
    idem = _idem("BUG-001", "graph", "fake")
    service.repo.create_task(
        task_id="T-STALE-1",
        idem_key=idem,
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="x",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir="runs/T-STALE-1",
    )
    service.repo.set_status_unless_terminal("T-STALE-1", "RUNNING")
    # 模拟崩溃残留:锁被前进程持有
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=600)

    assert service.recover_stale() == 1
    assert service.repo.get_task("T-STALE-1")["status"] == "NEEDS_REVIEW"
    # 锁已被清:同键任务立即可以创建,不再 409
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=5)
    service.lock.release(f"task:{idem}")
    service.shutdown()


def test_shutdown_converges_queued_and_cancels_running(tmp_path: Path, monkeypatch) -> None:
    """N-21 整改:停机传播——在途任务收到取消事件;被砍掉的排队任务
    行收敛 CANCELLED 且锁释放,不再永久滞留 RUNNING。"""
    import threading

    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
        max_workers=1,  # 单 worker:第二个任务必须真正排队
    )
    started = threading.Event()
    release = threading.Event()

    def _slow_execute(*args, **kwargs):
        started.set()
        release.wait(timeout=10)

    monkeypatch.setattr(service, "_execute", _slow_execute)
    service.create_task(bug_id="BUG-001", engine="graph", model="fake")
    assert started.wait(timeout=10)  # 占住 max_workers 池
    queued = service.create_task(bug_id="BUG-002", engine="graph", model="fake")
    assert queued[1]  # 新建且排队

    service.shutdown()
    # 排队任务被砍:行收敛 CANCELLED(没跑过),锁可立即重取
    idem2 = _idem("BUG-002", "graph", "fake")
    assert service.repo.get_task(queued[0]["task_id"])["status"] == "CANCELLED"
    assert service.lock.acquire(f"task:{idem2}", ttl_seconds=5)
    service.lock.release(f"task:{idem2}")
    _ = release  # 在途任务由解释器退出/事件驱动收敛,此处只验证排队语义
