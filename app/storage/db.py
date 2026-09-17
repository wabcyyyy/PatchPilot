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
  test_cmd TEXT,
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

CREATE TABLE IF NOT EXISTS test_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  round INTEGER,
  kind TEXT,
  passed INTEGER,
  failed INTEGER,
  errors INTEGER,
  exit_code INTEGER,
  report_path TEXT,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_test_runs_task ON test_runs(task_id);

CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT UNIQUE,
  bug_id TEXT,
  localized INTEGER,
  patch_applied INTEGER,
  final_resolved INTEGER,
  regression_introduced INTEGER,
  security_blocked INTEGER,
  rounds INTEGER,
  tokens INTEGER,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eval_bug ON evaluations(bug_id);
"""


# 旧库升级:evaluations 建表后补列(缺则 ALTER,已有则跳过)
_EVALUATION_MIGRATIONS: list[tuple[str, str]] = [
    ("cost_usd", "ALTER TABLE evaluations ADD COLUMN cost_usd REAL"),
    ("tokens_prompt", "ALTER TABLE evaluations ADD COLUMN tokens_prompt INTEGER"),
    ("tokens_completion", "ALTER TABLE evaluations ADD COLUMN tokens_completion INTEGER"),
]


def _migrate_evaluations(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}
    for column, statement in _EVALUATION_MIGRATIONS:
        if column not in existing:
            conn.execute(statement)
            conn.commit()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开(并初始化)数据库连接;check_same_thread 关闭,由 Repository 统一加锁。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_evaluations(conn)
    conn.commit()
    return conn
