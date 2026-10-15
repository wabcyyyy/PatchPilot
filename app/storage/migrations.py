"""SQLite 版本化迁移模块 (P01: 事务化 forward migrations 与持久化模型)。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2

# 终端状态集合，与 app.storage.repository.TERMINAL_STATUSES 保持严格一致
_TERMINAL_STATUSES_SQL = (
    "'FINISHED', 'INVALID_TASK', 'BUDGET_EXCEEDED', 'VERIFY_FAILED', 'PATCH_REJECTED',"
    " 'NEEDS_REVIEW', 'CANCELLED'"
)

_TASK_SPEC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_spec_json", "TEXT"),
    ("task_spec_hash", "TEXT"),
    ("task_spec_schema_version", "INTEGER"),
    ("stage", "TEXT"),
    ("last_event_at", "TEXT"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _migration_v1(conn: sqlite3.Connection) -> None:
    """V1 基线迁移：创建基础三表，补充任务契约与运行进度列，完成轨迹唯一索引。"""
    conn.executescript(
        """
CREATE TABLE IF NOT EXISTS tasks (
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
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_idem ON tasks(idem_key);

CREATE TABLE IF NOT EXISTS trajectory_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT,
  task_id TEXT NOT NULL,
  round INTEGER,
  state TEXT,
  tool TEXT,
  request_id TEXT,
  input TEXT,
  output_summary TEXT,
  duration_ms INTEGER,
  error TEXT,
  timestamp TEXT
);
CREATE INDEX IF NOT EXISTS idx_traj_task ON trajectory_events(task_id, id);

CREATE TABLE IF NOT EXISTS patches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  round INTEGER,
  diff_path TEXT,
  changed_files TEXT,
  gate_result TEXT,
  applied INTEGER
);
CREATE INDEX IF NOT EXISTS idx_patches_task ON patches(task_id);
"""
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, coltype in _TASK_SPEC_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {coltype}")

    _migrate_trajectory_dedup(conn)


def _migrate_trajectory_dedup(conn: sqlite3.Connection) -> None:
    """trajectory_events 按 (task_id, event_id) 去重并建唯一索引。"""
    index_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_traj_task_event'"
    ).fetchone()
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trajectory_events'"
    ).fetchone()
    if index_exists or not table_exists:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trajectory_events_duplicates AS"
        " SELECT * FROM trajectory_events WHERE 0"
    )
    dupes = conn.execute(
        "SELECT COUNT(*) FROM trajectory_events"
        " WHERE event_id IS NOT NULL"
        " AND id NOT IN (SELECT MIN(id) FROM trajectory_events"
        "               WHERE event_id IS NOT NULL GROUP BY task_id, event_id)"
    ).fetchone()[0]
    if dupes:
        conn.execute(
            "INSERT INTO trajectory_events_duplicates"
            " SELECT * FROM trajectory_events"
            " WHERE event_id IS NOT NULL"
            " AND id NOT IN (SELECT MIN(id) FROM trajectory_events"
            "               WHERE event_id IS NOT NULL GROUP BY task_id, event_id)"
        )
        conn.execute(
            "DELETE FROM trajectory_events"
            " WHERE event_id IS NOT NULL"
            " AND id NOT IN (SELECT MIN(id) FROM trajectory_events"
            "               WHERE event_id IS NOT NULL GROUP BY task_id, event_id)"
        )
    conn.execute(
        "CREATE UNIQUE INDEX uq_traj_task_event"
        " ON trajectory_events(task_id, event_id)"
        " WHERE event_id IS NOT NULL"
    )


def _migration_v2(conn: sqlite3.Connection) -> None:
    """V2 工业试点持久化模型：主体、凭据、审计、作业租约、配额、工件、任务归属与活动幂等索引。"""
    # 1. 扩展 tasks 表：owner 与 principal_id
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "owner" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN owner TEXT DEFAULT 'legacy-unowned'")
    if "principal_id" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN principal_id TEXT DEFAULT 'legacy-unowned'")

    # 回填旧数据：历史无主任务标 legacy-unowned，禁止自动归属给首个用户
    conn.execute("UPDATE tasks SET owner = 'legacy-unowned' WHERE owner IS NULL OR owner = ''")
    conn.execute(
        "UPDATE tasks SET principal_id = 'legacy-unowned'"
        " WHERE principal_id IS NULL OR principal_id = ''"
    )

    # 2. 活动任务幂等索引：仅约束非终态且 idem_key 非空的任务
    conn.execute(
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_active_principal_idem
ON tasks(principal_id, idem_key)
WHERE status NOT IN ({_TERMINAL_STATUSES_SQL}) AND idem_key IS NOT NULL;
"""
    )

    # 3. 主体与凭据表
    conn.executescript(
        """
CREATE TABLE IF NOT EXISTS principals (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  revoked_at TEXT,
  FOREIGN KEY(principal_id) REFERENCES principals(id)
);
CREATE INDEX IF NOT EXISTS idx_credentials_principal ON credentials(principal_id);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  task_id TEXT,
  request_id TEXT,
  timestamp TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  payload_hash TEXT,
  detail_json TEXT,
  result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_expires_at TEXT,
  fencing_token INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_prio ON jobs(status, priority, created_at);

CREATE TABLE IF NOT EXISTS quota_reservations (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  reserved_amount REAL NOT NULL DEFAULT 0.0,
  reserved_tokens INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_task ON quota_reservations(task_id);
CREATE INDEX IF NOT EXISTS idx_quota_principal ON quota_reservations(principal_id);

CREATE TABLE IF NOT EXISTS artifact_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retention_policy TEXT,
  is_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifact_records(task_id);
"""
    )


MigrationStep = tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: tuple[MigrationStep, ...] = (
    (1, "baseline_schema_and_task_spec", _migration_v1),
    (2, "principals_jobs_quota_audit_and_owner", _migration_v2),
)


def run_migrations(conn: sqlite3.Connection) -> int:
    """执行所有未应用的 forward migrations。

    保证：
    1. 迁移在事务内串行运行 (BEGIN IMMEDIATE)；
    2. 幂等：重复执行不产生重复影响；
    3. 失败原子回滚，不留半迁移状态。
    返回最新应用的 schema version。
    """
    if conn.in_transaction:
        conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  description TEXT NOT NULL
);
"""
        )
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        latest = 0
        for version, desc, func in MIGRATIONS:
            if version not in applied:
                log.info("Applying schema migration v%d: %s", version, desc)
                func(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                    (version, _now(), desc),
                )
            latest = max(latest, version)
        conn.commit()
        return latest
    except Exception:
        conn.rollback()
        raise
