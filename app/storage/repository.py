"""仓储层:任务、轨迹、补丁、测试结果、评测汇总的持久化访问。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.db import connect

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _task_out(row: sqlite3.Row | dict | None) -> dict[str, Any] | None:
    """对外统一暴露 task_id 字段(库内主键列名是 id)。"""
    if row is None:
        return None
    item = dict(row)
    item.setdefault("task_id", item.get("id"))
    return item


class Repository:
    """线程安全的 SQLite 访问层(SQLite 并发写弱,统一锁保护)。"""

    def __init__(self, db_path: Path | str) -> None:
        self._conn: sqlite3.Connection = connect(db_path)
        self._lock = threading.Lock()

    # ---------- tasks ----------

    def create_task(
        self,
        *,
        task_id: str,
        idem_key: str,
        bug_id: str,
        repo_path: str,
        issue_text: str,
        max_rounds: int,
        engine: str,
        model_provider: str,
        run_dir: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks (id, idem_key, bug_id, repo_path, issue_text, max_rounds,"
                " engine, model_provider, status, run_dir, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    idem_key,
                    bug_id,
                    repo_path,
                    issue_text,
                    max_rounds,
                    engine,
                    model_provider,
                    "QUEUED",
                    run_dir,
                    _now(),
                ),
            )

    def find_by_idem_key(self, idem_key: str) -> dict[str, Any] | None:
        """返回最近一条同键任务;是否"在途"由调用方按状态判断(终态任务允许重跑)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE idem_key = ? ORDER BY created_at DESC LIMIT 1",
                (idem_key,),
            ).fetchone()
        return _task_out(row)

    def update_task_status(self, task_id: str, status: str, verdict: str | None = None) -> None:
        finished = (
            _now()
            if status in {"FINISHED", "NEEDS_REVIEW", "CANCELLED"}
            or status
            in {
                "INVALID_TASK",
                "BUDGET_EXCEEDED",
                "VERIFY_FAILED",
                "PATCH_REJECTED",
            }
            else None
        )
        with self._lock, self._conn:
            if finished:
                self._conn.execute(
                    "UPDATE tasks SET status=?, verdict=?, finished_at=? WHERE id=?",
                    (status, verdict, finished, task_id),
                )
            else:
                self._conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_out(row)

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_task_out(r) for r in rows]

    def recover_stale_running(self) -> int:
        """服务启动时把 RUNNING/QUEUED 的僵尸任务标记为 NEEDS_REVIEW,返回数量。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW', finished_at=?"
                " WHERE status IN ('RUNNING','QUEUED')",
                (_now(),),
            )
            count = cur.rowcount
        if count:
            log.warning("recovered %d stale running task(s) as NEEDS_REVIEW", count)
        return count

    # ---------- trajectory ----------

    def insert_events(self, task_id: str, events: list[dict[str, Any]]) -> int:
        rows = [
            (
                e.get("event_id"),
                task_id,
                e.get("round"),
                e.get("state"),
                e.get("tool"),
                e.get("request_id"),
                _dump(e.get("input")),
                _dump(e.get("output_summary")),
                e.get("duration_ms"),
                e.get("error"),
                e.get("timestamp"),
            )
            for e in events
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO trajectory_events (event_id, task_id, round, state, tool, request_id,"
                " input, output_summary, duration_ms, error, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def get_trajectory(
        self, task_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trajectory_events WHERE task_id = ? ORDER BY id LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("input", "output_summary"):
                item[key] = json.loads(item[key]) if item[key] else None
            out.append(item)
        return out

    # ---------- patches / test_runs / evaluations ----------

    def insert_patch(
        self,
        *,
        task_id: str,
        round_no: int,
        diff_path: str,
        changed_files: list[str],
        gate_result: str,
        applied: bool,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO patches (task_id, round, diff_path, changed_files, gate_result, applied)"
                " VALUES (?,?,?,?,?,?)",
                (
                    task_id,
                    round_no,
                    diff_path,
                    json.dumps(changed_files, ensure_ascii=False),
                    gate_result,
                    int(applied),
                ),
            )

    def insert_test_run(
        self,
        *,
        task_id: str,
        round_no: int,
        kind: str,
        passed: int,
        failed: int,
        errors: int,
        exit_code: int,
        report_path: str,
        duration_ms: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO test_runs (task_id, round, kind, passed, failed, errors, exit_code,"
                " report_path, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    round_no,
                    kind,
                    passed,
                    failed,
                    errors,
                    exit_code,
                    report_path,
                    duration_ms,
                ),
            )

    def upsert_evaluation(self, **fields: Any) -> None:
        keys = [
            "task_id",
            "bug_id",
            "localized",
            "patch_applied",
            "final_resolved",
            "regression_introduced",
            "security_blocked",
            "rounds",
            "tokens",
            "duration_ms",
            "cost_usd",
            "tokens_prompt",
            "tokens_completion",
        ]
        values = [fields.get(k) for k in keys]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO evaluations ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})"
                " ON CONFLICT(task_id) DO UPDATE SET "
                + ",".join(f"{k}=excluded.{k}" for k in keys[1:]),
                values,
            )

    def list_evaluations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM evaluations ORDER BY bug_id").fetchall()
        return [dict(r) for r in rows]
