"""SQLite 连接与建表(企划书第 6 节五张表)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
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

-- test_runs 表已删(P1-5 整改,二选一取删除):零生产调用方,每次 pytest 的
-- 完整结果已落在 run_dir/reports/*.xml,库里再造一份只会是永远为空的空表
--
-- evaluations 表已删(P3-8 整改,P1-5 同先例):唯一写方是任务收尾的
-- upsert_evaluation,全仓零生产读方;取消×自然完成时它还会与 tasks/report.json
-- 三处分裂(审计 R2-Q1)。读口径:tasks = 服务生命周期真相,
-- report.json = 引擎判定取证,平台口径以 tasks 为准(design.md §7)。
"""


# S05a(F4):tasks 表增量列——规范任务契约(spec)的 JSON、哈希与 schema 版本。
# SQLite 的 ALTER TABLE 不能重复执行,这里按 PRAGMA 现状补列:迁移可重复
# (已存在即跳过)、事务化(三条要么全加要么全不加),旧行新列为 NULL、照常可查询。
_TASK_SPEC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("task_spec_json", "TEXT"),
    ("task_spec_hash", "TEXT"),
    ("task_spec_schema_version", "INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if all(name in existing for name, _ in _TASK_SPEC_COLUMNS):
        return
    with conn:
        for name, coltype in _TASK_SPEC_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {coltype}")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开(并初始化)数据库连接;check_same_thread 关闭,由 Repository 统一加锁。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn
