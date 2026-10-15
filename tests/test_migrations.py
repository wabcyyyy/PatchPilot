"""P01 存储层迁移与持久化模型测试。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.storage.db import connect
from app.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    run_migrations,
)
from app.storage.repository import Repository


def test_migrations_from_scratch(tmp_path: Path) -> None:
    """全新空库执行迁移，验证 schema_migrations 记录与所有新增表结构。"""
    db_file = tmp_path / "fresh.sqlite3"
    conn = connect(db_file)

    versions = [
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == [1, 2]

    # 验证关键表存在
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    expected_tables = {
        "tasks",
        "trajectory_events",
        "patches",
        "schema_migrations",
        "principals",
        "credentials",
        "audit_events",
        "jobs",
        "quota_reservations",
        "artifact_records",
    }
    assert expected_tables.issubset(tables)

    # 验证 tasks 表关键列
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "owner" in cols
    assert "principal_id" in cols
    conn.close()


def test_migrations_idempotency(tmp_path: Path) -> None:
    """重复运行迁移无害且幂等，版本不重复插入。"""
    db_file = tmp_path / "idem.sqlite3"
    conn = connect(db_file)
    count1 = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    # 再次显式调用
    latest = run_migrations(conn)
    assert latest == CURRENT_SCHEMA_VERSION
    count2 = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count1 == count2
    conn.close()


def test_legacy_database_upgrade(tmp_path: Path) -> None:
    """从缺少版本记录与主权列的旧库升级：历史无主任务标记为 legacy-unowned，不自动赋权。"""
    db_file = tmp_path / "legacy.sqlite3"
    # 手动建立旧版本 v1 风格的库（无 schema_migrations 表，tasks 表无 owner/principal_id 列）
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.execute(
        """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  idem_key TEXT,
  bug_id TEXT NOT NULL,
  repo_path TEXT,
  issue_text TEXT,
  max_rounds INTEGER,
  engine TEXT,
  model_provider TEXT,
  status TEXT NOT NULL,
  verdict TEXT,
  run_dir TEXT,
  created_at TEXT,
  finished_at TEXT
);
"""
    )
    raw_conn.execute(
        "INSERT INTO tasks (id, idem_key, bug_id, status, created_at)"
        " VALUES ('OLD-1', 'idem-legacy', 'BUG-001', 'FINISHED', '2026-10-01T00:00:00Z')"
    )
    raw_conn.commit()
    raw_conn.close()

    # 通过标准 connect 升级
    repo = Repository(db_file)
    task = repo.get_task("OLD-1")
    assert task is not None
    assert task["owner"] == "legacy-unowned"
    assert task["principal_id"] == "legacy-unowned"

    # 新建任务默认携带归属
    repo.create_task(
        task_id="NEW-1",
        idem_key="idem-new",
        bug_id="BUG-002",
        repo_path="bugs/BUG-002/repo",
        issue_text="test",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir="runs/NEW-1",
        owner="team-alpha",
        principal_id="principal-1",
    )
    new_task = repo.get_task("NEW-1")
    assert new_task["owner"] == "team-alpha"
    assert new_task["principal_id"] == "principal-1"

    # 按 owner 筛选
    legacy_tasks = repo.list_tasks(owner="legacy-unowned")
    assert len(legacy_tasks) == 1
    assert legacy_tasks[0]["id"] == "OLD-1"

    alpha_tasks = repo.list_tasks(owner="team-alpha")
    assert len(alpha_tasks) == 1
    assert alpha_tasks[0]["id"] == "NEW-1"


def test_active_task_idempotency_constraint(tmp_path: Path) -> None:
    """活动任务按 principal+idem_key 强约束唯一性，终态任务允许重跑。"""
    db_file = tmp_path / "idem_constraint.sqlite3"
    repo = Repository(db_file)

    # 1. 插入活动任务 P1, idem-A
    repo.create_task(
        task_id="T1",
        idem_key="idem-A",
        bug_id="BUG-001",
        repo_path="repo",
        issue_text="issue",
        max_rounds=3,
        engine="graph",
        model_provider="fake",
        run_dir="runs/T1",
        owner="user-1",
        principal_id="p1",
    )

    # 同一主体同一 idem_key，活动状态下重复插入应抛 IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_task(
            task_id="T2",
            idem_key="idem-A",
            bug_id="BUG-001",
            repo_path="repo",
            issue_text="issue",
            max_rounds=3,
            engine="graph",
            model_provider="fake",
            run_dir="runs/T2",
            owner="user-1",
            principal_id="p1",
        )

    # 2. 不同主体相同 idem_key，允许并行存在（主体间隔离）
    repo.create_task(
        task_id="T3",
        idem_key="idem-A",
        bug_id="BUG-001",
        repo_path="repo",
        issue_text="issue",
        max_rounds=3,
        engine="graph",
        model_provider="fake",
        run_dir="runs/T3",
        owner="user-2",
        principal_id="p2",
    )
    assert repo.get_task("T3") is not None

    # 3. 将 T1 置为终态 (FINISHED) 后，同一主体允许重新提交同 idem_key 任务
    repo.finalize_task("T1", "FINISHED", "resolved")
    repo.create_task(
        task_id="T4",
        idem_key="idem-A",
        bug_id="BUG-001",
        repo_path="repo",
        issue_text="issue",
        max_rounds=3,
        engine="graph",
        model_provider="fake",
        run_dir="runs/T4",
        owner="user-1",
        principal_id="p1",
    )
    assert repo.get_task("T4") is not None


def test_failed_migration_atomic_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """迁移中途失败时事务原子回滚，无残留列或部分表。"""
    db_file = tmp_path / "rollback.sqlite3"
    conn = sqlite3.connect(str(db_file))

    def _failing_migration(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE partial_table (id INT)")
        raise RuntimeError("simulated migration explosion")

    fake_migrations = [*MIGRATIONS, (999, "failing_migration", _failing_migration)]
    monkeypatch.setattr("app.storage.migrations.MIGRATIONS", tuple(fake_migrations))

    with pytest.raises(RuntimeError, match="simulated migration explosion"):
        run_migrations(conn)

    # 验证 partial_table 不存在（被回滚）
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_table'"
    ).fetchone()
    assert exists is None
    conn.close()


def test_concurrent_migrations_serialize(tmp_path: Path) -> None:
    """多线程并发打开连接并迁移，通过 busy_timeout 与 IMMEDIATE 事务串行化，均成功完成。"""
    db_file = tmp_path / "concurrent.sqlite3"
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            c = connect(db_file)
            c.execute("SELECT COUNT(*) FROM tasks").fetchone()
            c.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发迁移出现异常: {errors}"
    final_conn = connect(db_file)
    v = final_conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert v == CURRENT_SCHEMA_VERSION
    final_conn.close()
