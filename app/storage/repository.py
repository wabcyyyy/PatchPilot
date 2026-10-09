"""仓储层:任务、轨迹、补丁、测试结果、评测汇总的持久化访问。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.db import connect

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# 终态集合的唯一权威定义(P2-2/N-7 整改):此前 service/state/repository 各持一份、
# 内容不一(state 版不含 CANCELLED),任何一份漂移都会让取消或终态判定失真。
# 引擎层不产出 CANCELLED(由 service 收敛),DB 层必须含它。
TERMINAL_STATUSES = frozenset(
    {
        "FINISHED",
        "INVALID_TASK",
        "BUDGET_EXCEEDED",
        "VERIFY_FAILED",
        "PATCH_REJECTED",
        "NEEDS_REVIEW",
        "CANCELLED",
    }
)


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

    def set_task_spec(
        self, task_id: str, *, spec_json: str, spec_hash: str, schema_version: int
    ) -> bool:
        """回填规范任务契约(S05a);仅非终态可写,返回 False=任务已终态。

        spec_json 必须已经是规范序列化形态(canonical_json),哈希由调用方按
        app.task_spec 的同一算法计算——本层不做第二种哈希口径。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET task_spec_json=?, task_spec_hash=?, task_spec_schema_version=?"
                " WHERE id=? AND status NOT IN"
                f" ({','.join('?' * len(TERMINAL_STATUSES))})",
                (spec_json, spec_hash, schema_version, task_id, *TERMINAL_STATUSES),
            )
        return cur.rowcount > 0

    def get_task_spec(self, task_id: str) -> dict[str, Any] | None:
        """读契约三列;未落契约的旧行返回 None(调用方按"不可自动恢复"处理)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_spec_json, task_spec_hash, task_spec_schema_version"
                " FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None or row["task_spec_json"] is None:
            return None
        return {
            "task_spec_json": row["task_spec_json"],
            "task_spec_hash": row["task_spec_hash"],
            "task_spec_schema_version": row["task_spec_schema_version"],
        }

    def set_status_unless_terminal(self, task_id: str, status: str) -> bool:
        """非终态流转写入(RUNNING 等,N-7/R2 整改):任务已到终态(含并发取消)
        时写入不生效并返回 False——此前 create_task 置 RUNNING 是无条件写,
        可以把并发 cancel 写入的 CANCELLED"复活"成 RUNNING。"""
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE tasks SET status=? WHERE id=? AND status NOT IN ({placeholders})",
                (status, task_id, *TERMINAL_STATUSES),
            )
        return cur.rowcount > 0

    def finalize_task(self, task_id: str, status: str, verdict: str | None = None) -> bool:
        """终态回写(N-7/P3-4 整改):任一终态不可覆写,谁先到终态谁赢。

        此前守卫仅 `!= 'CANCELLED'`:只防"完成覆盖取消",防不了终态之间的
        互相覆写——多进程共库时 recover_stale(B 进程)把 A 的在途任务收敛
        NEEDS_REVIEW 后,A 跑完 finalize(FINISHED) 会把 NEEDS_REVIEW"复活"。
        现在守卫下沉为 NOT IN 全终态的单条原子语句,以 rowcount 判定输赢。
        多进程部署前仍需重审(design.md §7)。
        """
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE tasks SET status=?, verdict=?, finished_at=?"
                f" WHERE id=? AND status NOT IN ({placeholders})",
                (status, verdict, _now(), task_id, *TERMINAL_STATUSES),
            )
        return cur.rowcount > 0

    def cancel_task_row(self, task_id: str) -> bool:
        """取消(N-7 整改):仅当任务未到终态才生效;返回 False 表示已是终态。"""
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE tasks SET status='CANCELLED', verdict='cancelled', finished_at=?"
                f" WHERE id=? AND status NOT IN ({placeholders})",
                (_now(), task_id, *TERMINAL_STATUSES),
            )
        return cur.rowcount > 0

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

    def list_tasks_finished_before(self, status: str, finished_before: str) -> list[dict[str, Any]]:
        """终态早于给定 ISO 时间戳的任务行(P3-12 启动 Grace 扫描的读方)。

        finished_at 与入参同为 UTC isoformat 同精度字符串,可直接字典序比较。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status=? AND finished_at IS NOT NULL"
                " AND finished_at < ? ORDER BY finished_at",
                (status, finished_before),
            ).fetchall()
        return [_task_out(r) for r in rows]

    def list_stale_running(self) -> list[dict[str, Any]]:
        """RUNNING/QUEUED 的整行(M6 启动恢复的读方)。

        恢复要按行决定去向(有可用快照 → 重新入队续跑;没有 → 判死),所以需要的不只是
        idem_key。只读方法,不改状态——写仍集中在 `recover_stale_running`。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('RUNNING','QUEUED')"
            ).fetchall()
        return [_task_out(r) for r in rows if r is not None]

    def recover_stale_running(self, exclude_task_ids: Sequence[str] = ()) -> list[str]:
        """服务启动时把 RUNNING/QUEUED 的僵尸任务标记为 NEEDS_REVIEW。

        N-20 整改:返回被恢复行的 idem_key,调用方(service)据此同步清掉
        Redis 里的残留任务锁——否则重启后同键重试会被 409 卡死到 TTL。
        M6:`exclude_task_ids` 里的任务**不判死**(将由 service 重新入队续跑),
        因此既不出现在返回值里、也不被 UPDATE 命中;传空元组时与本方法引入以来
        的行为逐字相同(既有测试钉住那条路径)。
        """
        excluded = list(exclude_task_ids)
        clause = ""
        params: tuple[Any, ...] = ()
        if excluded:
            clause = f" AND id NOT IN ({','.join('?' * len(excluded))})"
            params = tuple(excluded)
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT idem_key FROM tasks WHERE status IN ('RUNNING','QUEUED')" + clause,
                params,
            ).fetchall()
            stale_keys = [r["idem_key"] for r in rows if r["idem_key"]]
            self._conn.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW', finished_at=?"
                " WHERE status IN ('RUNNING','QUEUED')" + clause,
                (_now(), *params),
            )
        if stale_keys:
            log.warning("recovered %d stale running task(s) as NEEDS_REVIEW", len(stale_keys))
        return stale_keys

    # ---------- trajectory ----------

    def insert_events(self, task_id: str, events: list[dict[str, Any]]) -> int:
        """批量入轨迹(收尾补录口径,S07 起 INSERT OR IGNORE):

        (task_id, event_id) 唯一索引兜底——实时 sink 已入账的事件在此幂等跳过,
        返回**实际新插入**的行数;补录数 > 0 即运行期 sink 有缺口(可观测降级)。
        """
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
        inserted = 0
        with self._lock, self._conn:
            for row in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO trajectory_events"
                    " (event_id, task_id, round, state, tool, request_id,"
                    " input, output_summary, duration_ms, error, timestamp)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                inserted += cur.rowcount
        return inserted

    def upsert_event_live(self, task_id: str, event: dict[str, Any]) -> bool:
        """运行中实时入账单条事件并推进进度列(S07);终态行不复活。

        返回 False = 重复事件或任务已终态(进度列不再推进);调用方记日志继续,
        收尾由 insert_events 补录兜底。刻意不持有 Tracker 锁(锁顺序倒置防线)。
        """
        inserted = self.insert_events(task_id, [event])
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET stage=?, last_event_at=?"
                " WHERE id=? AND status NOT IN"
                f" ({','.join('?' * len(TERMINAL_STATUSES))})",
                (
                    str(event.get("state") or ""),
                    str(event.get("timestamp") or ""),
                    task_id,
                    *TERMINAL_STATUSES,
                ),
            )
        return inserted > 0 and cur.rowcount > 0

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

    # ---------- patches ----------
    # (test_runs/evaluations 表已删,见 P1-5/P3-8 注记)

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

    # insert_test_run 已删(P1-5 整改):test_runs 表零生产调用方,
    # pytest 完整结果以 junit xml 形式落在 run_dir/reports/,不入库
    # upsert_evaluation/list_evaluations 已删(P3-8 整改,P1-5 同先例):
    # evaluations 表零生产读方,取消×自然完成时与 tasks/report.json 三处分裂;
    # 读口径 tasks=生命周期真相、report.json=引擎取证(design.md §7)
