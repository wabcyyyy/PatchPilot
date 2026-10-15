"""仓储层:任务、轨迹、补丁、测试结果、评测汇总的持久化访问。"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
        self._lock = threading.RLock()

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
        owner: str = "legacy-unowned",
        principal_id: str = "legacy-unowned",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks (id, idem_key, bug_id, repo_path, issue_text, max_rounds,"
                " engine, model_provider, status, run_dir, created_at, owner, principal_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    owner,
                    principal_id,
                ),
            )

    def find_by_idem_key(
        self, idem_key: str, principal_id: str | None = None
    ) -> dict[str, Any] | None:
        """返回最近一条同键任务;是否"在途"由调用方按状态判断(终态任务允许重跑)。"""
        with self._lock:
            if principal_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE idem_key = ? AND principal_id = ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (idem_key, principal_id),
                ).fetchone()
            else:
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

    def set_status_unless_terminal(
        self,
        task_id: str,
        status: str,
        *,
        fencing_token: int | None = None,
    ) -> bool:
        """非终态流转写入(RUNNING 等,N-7/R2/P12 整改):任务已到终态(含并发取消)
        或 fencing_token 失效时不生效并返回 False。"""
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock, self._conn:
            if fencing_token is not None:
                job_row = self._conn.execute(
                    "SELECT fencing_token FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
                if job_row and int(job_row[0] or 0) != fencing_token:
                    log.warning(
                        "task %s: set_status_unless_terminal rejected (token caller=%d, db=%d)",
                        task_id,
                        fencing_token,
                        int(job_row[0] or 0),
                    )
                    return False
            cur = self._conn.execute(
                f"UPDATE tasks SET status=? WHERE id=? AND status NOT IN ({placeholders})",
                (status, task_id, *TERMINAL_STATUSES),
            )
        return cur.rowcount > 0

    def finalize_task(
        self,
        task_id: str,
        status: str,
        verdict: str | None = None,
        *,
        fencing_token: int | None = None,
    ) -> bool:
        """终态回写(N-7/P3-4/P12 整改):任一终态不可覆写,谁先到终态谁赢。

        P12: 支持 fencing_token 校验。若当前 job 已被更高 token 抢占，旧 worker
        写入直接拒绝，防止僵尸 worker 覆盖新 worker 的判定。
        """
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._lock, self._conn:
            if fencing_token is not None:
                job_row = self._conn.execute(
                    "SELECT fencing_token FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
                if job_row and int(job_row[0] or 0) != fencing_token:
                    log.warning(
                        "task %s: finalize_task rejected (token caller=%d, db=%d)",
                        task_id,
                        fencing_token,
                        int(job_row[0] or 0),
                    )
                    return False
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

    def list_tasks(
        self,
        limit: int = 50,
        owner: str | None = None,
        principal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if owner is not None and principal_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE owner = ? AND principal_id = ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (owner, principal_id, limit),
                ).fetchall()
            elif owner is not None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                    (owner, limit),
                ).fetchall()
            elif principal_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE principal_id = ? ORDER BY created_at DESC LIMIT ?",
                    (principal_id, limit),
                ).fetchall()
            else:
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
        """RUNNING/QUEUED/WAITING_INPUT 的整行(M6 启动恢复的读方;G10 起含等待批准的
        issue-only 任务——等待也是可恢复态,不能当僵尸判死)。

        P12 整改: 仅处理明确失主任务。
        若任务关联的 job 正由活实例持有有效租约(status='claimed' 且 lease_expires_at > now),
        或处于持久队列等待中(status='pending'),不得视为僵尸判死。
        """
        now_str = _now()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.* FROM tasks t
                LEFT JOIN jobs j ON t.id = j.task_id
                WHERE t.status IN ('RUNNING','QUEUED','WAITING_INPUT')
                  AND (
                    j.id IS NULL
                    OR (j.status = 'claimed' AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at <= ?)
                  )
                """,
                (now_str,),
            ).fetchall()
        return [_task_out(r) for r in rows if r is not None]

    def recover_stale_running(self, exclude_task_ids: Sequence[str] = ()) -> list[str]:
        """服务启动时把失主的 RUNNING/QUEUED 僵尸任务标记为 NEEDS_REVIEW。

        N-20 整改:返回被恢复行的 idem_key,调用方(service)据此同步清掉
        Redis 里的残留任务锁——否则重启后同键重试会被 409 卡死到 TTL。
        M6:`exclude_task_ids` 里的任务**不判死**(将由 service 重新入队续跑)。
        P12 整改: 仅收敛失主任务(无 job 或租约已过期的 job)，不伤及活 worker 的任务。
        """
        now_str = _now()
        excluded = list(exclude_task_ids)
        clause = ""
        params: list[Any] = [now_str]
        if excluded:
            clause = f" AND t.id NOT IN ({','.join('?' * len(excluded))})"
            params.extend(excluded)
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT t.idem_key, t.id FROM tasks t
                LEFT JOIN jobs j ON t.id = j.task_id
                WHERE t.status IN ('RUNNING','QUEUED','WAITING_INPUT')
                  AND (
                    j.id IS NULL
                    OR (j.status = 'claimed' AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at <= ?)
                  )
                """
                + clause,
                tuple(params),
            ).fetchall()
            stale_keys = [r["idem_key"] for r in rows if r["idem_key"]]
            stale_task_ids = [r["id"] for r in rows if r["id"]]
            if stale_task_ids:
                placeholders = ",".join("?" * len(stale_task_ids))
                self._conn.execute(
                    f"UPDATE tasks SET status='NEEDS_REVIEW', finished_at=?"
                    f" WHERE id IN ({placeholders})",
                    (_now(), *stale_task_ids),
                )
                self._conn.execute(
                    f"UPDATE jobs SET status='failed', lease_expires_at=NULL, updated_at=?"
                    f" WHERE task_id IN ({placeholders}) AND status = 'claimed'",
                    (_now(), *stale_task_ids),
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
        return self._decode_event_rows(rows)

    def events_since(self, task_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """自增 id > after_id 的事件行(G12 事件流的增量读方;与轨迹同一张表、同一组事实)。

        行里带自增 `id`——SSE 的 `id:` 与 Last-Event-ID 续传就靠它;
        `after_id=0` 从头读,与 get_trajectory(offset=0) 同源。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trajectory_events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                (task_id, max(int(after_id or 0), 0), limit),
            ).fetchall()
        return self._decode_event_rows(rows)

    @staticmethod
    def _decode_event_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
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

    # ---------- principals & credentials (P02) ----------

    def create_principal(
        self, *, principal_id: str, name: str, role: str = "member"
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO principals (id, name, role, created_at, is_active) VALUES (?, ?, ?, ?, 1)",
                (principal_id, name, role, _now()),
            )
        return self.get_principal(principal_id)  # type: ignore

    def get_principal(self, principal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM principals WHERE id = ?", (principal_id,)
            ).fetchone()
        return dict(row) if row else None

    def issue_credential(
        self,
        *,
        principal_id: str,
        raw_key: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        if not raw_key:
            raw_key = f"pp_live_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()
        prefix = raw_key[:12]
        cred_id = f"cr_{uuid.uuid4().hex[:12]}"
        created = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO credentials (id, principal_id, key_hash, prefix, created_at, expires_at, revoked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (cred_id, principal_id, key_hash, prefix, created, expires_at),
            )
            row = self._conn.execute(
                "SELECT id, principal_id, prefix, created_at, expires_at, revoked_at FROM credentials WHERE id = ?",
                (cred_id,),
            ).fetchone()
        return dict(row), raw_key

    def authenticate_credential(self, raw_key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not raw_key or not isinstance(raw_key, str):
            return None
        key_hash = hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()
        now_str = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials WHERE key_hash = ? AND revoked_at IS NULL",
                (key_hash,),
            ).fetchone()
            if not row:
                return None
            cred = dict(row)
            if cred.get("expires_at") and cred["expires_at"] <= now_str:
                return None
            p_row = self._conn.execute(
                "SELECT * FROM principals WHERE id = ? AND is_active = 1",
                (cred["principal_id"],),
            ).fetchone()
            if not p_row:
                return None
            principal = dict(p_row)
        return principal, cred

    def revoke_credential(self, credential_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), credential_id),
            )
        return cur.rowcount > 0

    def rotate_credential(
        self, old_credential_id: str, grace_seconds: int = 0
    ) -> tuple[dict[str, Any], str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials WHERE id = ?", (old_credential_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"credential not found: {old_credential_id}")
            old_cred = dict(row)
            if old_cred.get("revoked_at") is not None:
                raise ValueError(f"cannot rotate revoked credential: {old_credential_id}")

        new_cred, new_raw_key = self.issue_credential(
            principal_id=old_cred["principal_id"],
            expires_at=old_cred.get("expires_at"),
        )
        with self._lock, self._conn:
            if grace_seconds > 0:
                grace_expiry = (datetime.now(UTC) + timedelta(seconds=grace_seconds)).isoformat(
                    timespec="milliseconds"
                )
                self._conn.execute(
                    "UPDATE credentials SET expires_at = ? WHERE id = ?",
                    (grace_expiry, old_credential_id),
                )
            else:
                self._conn.execute(
                    "UPDATE credentials SET revoked_at = ? WHERE id = ?",
                    (_now(), old_credential_id),
                )
        return new_cred, new_raw_key

    def list_credentials(self, principal_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if principal_id is not None:
                rows = self._conn.execute(
                    "SELECT id, principal_id, prefix, created_at, expires_at, revoked_at"
                    " FROM credentials WHERE principal_id = ? ORDER BY created_at DESC",
                    (principal_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, principal_id, prefix, created_at, expires_at, revoked_at"
                    " FROM credentials ORDER BY created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_audit_event(
        self,
        *,
        actor_id: str,
        action: str,
        task_id: str | None = None,
        request_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        payload_hash: str | None = None,
        detail_json: str | None = None,
        result: str = "success",
    ) -> str:
        """记录不可变结构化审计日志 (P03)。"""
        event_id = f"aud-{uuid.uuid4().hex[:12]}"
        timestamp = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
INSERT INTO audit_events (
  event_id, actor_id, action, task_id, request_id, timestamp,
  target_type, target_id, payload_hash, detail_json, result
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
                (
                    event_id,
                    actor_id,
                    action,
                    task_id,
                    request_id,
                    timestamp,
                    target_type,
                    target_id,
                    payload_hash,
                    detail_json,
                    result,
                ),
            )
        return event_id

    def list_audit_events(
        self,
        *,
        task_id: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit_events WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if action is not None:
            query += " AND action = ?"
            params.append(action)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # ---------- P10-2: 主体/组织额度预留、结算与对账 ----------

    def reserve_quota(
        self,
        *,
        task_id: str,
        principal_id: str,
        amount_usd: float = 0.0,
        tokens: int = 0,
        daily_budget_usd: float = 0.0,
        daily_token_budget: int = 0,
    ) -> dict[str, Any]:
        """原子预留主体配额 (P10-2)。

        在同一锁与事务内统计当日累计已占用额度 (reserved + settled),
        并发抢最后额度只有一方能成功。超限抛出 QuotaExceededError。
        """
        start_of_today = datetime.now(UTC).strftime("%Y-%m-%d 00:00:00")
        reservation_id = f"res-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(reserved_amount), 0.0) AS total_amount,
                    COALESCE(SUM(reserved_tokens), 0) AS total_tokens
                FROM quota_reservations
                WHERE principal_id = ?
                  AND status IN ('reserved', 'settled')
                  AND created_at >= ?
                """,
                (principal_id, start_of_today),
            ).fetchone()
            used_amount = float(row[0]) if row else 0.0
            used_tokens = int(row[1]) if row else 0

            if daily_budget_usd > 0.0 and (used_amount + amount_usd) > daily_budget_usd:
                from app.errors import QuotaExceededError

                raise QuotaExceededError(
                    f"principal '{principal_id}' daily budget exceeded: "
                    f"requested {amount_usd:.4f} USD, currently used {used_amount:.4f} USD, "
                    f"daily limit {daily_budget_usd:.4f} USD"
                )

            if daily_token_budget > 0 and (used_tokens + tokens) > daily_token_budget:
                from app.errors import QuotaExceededError

                raise QuotaExceededError(
                    f"principal '{principal_id}' daily token budget exceeded: "
                    f"requested {tokens} tokens, currently used {used_tokens} tokens, "
                    f"daily limit {daily_token_budget} tokens"
                )

            self._conn.execute(
                """
                INSERT INTO quota_reservations (
                    id, task_id, principal_id, reserved_amount, reserved_tokens, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (reservation_id, task_id, principal_id, amount_usd, tokens, now),
            )
        return {
            "id": reservation_id,
            "task_id": task_id,
            "principal_id": principal_id,
            "reserved_amount": amount_usd,
            "reserved_tokens": tokens,
            "status": "reserved",
            "created_at": now,
        }

    def settle_quota(
        self,
        *,
        task_id: str,
        actual_amount_usd: float,
        actual_tokens: int,
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        """结算任务配额 (P10-2/P12)。"""
        now = _now()
        with self._lock, self._conn:
            if fencing_token is not None:
                job_row = self._conn.execute(
                    "SELECT fencing_token FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
                if job_row and int(job_row[0] or 0) != fencing_token:
                    log.warning(
                        "task %s: settle_quota rejected (caller=%d, db=%d)",
                        task_id,
                        fencing_token,
                        int(job_row[0] or 0),
                    )
                    return None
            row = self._conn.execute(
                "SELECT id FROM quota_reservations WHERE task_id = ? AND status = 'reserved'",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            res_id = row[0]
            self._conn.execute(
                """
                UPDATE quota_reservations
                SET status = 'settled',
                    reserved_amount = ?,
                    reserved_tokens = ?,
                    settled_at = ?
                WHERE id = ?
                """,
                (actual_amount_usd, actual_tokens, now, res_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM quota_reservations WHERE id = ?", (res_id,)
            ).fetchone()
            return dict(updated) if updated else None

    def release_quota(
        self,
        task_id: str,
        *,
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        """释放未使用的预留配额 (P10-2/P12)。"""
        now = _now()
        with self._lock, self._conn:
            if fencing_token is not None:
                job_row = self._conn.execute(
                    "SELECT fencing_token FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
                if job_row and int(job_row[0] or 0) != fencing_token:
                    log.warning(
                        "task %s: release_quota rejected (caller=%d, db=%d)",
                        task_id,
                        fencing_token,
                        int(job_row[0] or 0),
                    )
                    return None
            row = self._conn.execute(
                "SELECT id FROM quota_reservations WHERE task_id = ? AND status = 'reserved'",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            res_id = row[0]
            self._conn.execute(
                """
                UPDATE quota_reservations
                SET status = 'released',
                    reserved_amount = 0.0,
                    reserved_tokens = 0,
                    settled_at = ?
                WHERE id = ?
                """,
                (now, res_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM quota_reservations WHERE id = ?", (res_id,)
            ).fetchone()
            return dict(updated) if updated else None

    def get_quota_reservation(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quota_reservations WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_principal_quota_usage(
        self,
        principal_id: str,
        *,
        since: str | None = None,
    ) -> dict[str, Any]:
        since_ts = since or datetime.now(UTC).strftime("%Y-%m-%d 00:00:00")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(reserved_amount), 0.0) AS used_amount_usd,
                    COALESCE(SUM(reserved_tokens), 0) AS total_tokens,
                    COUNT(*) AS count
                FROM quota_reservations
                WHERE principal_id = ?
                  AND status IN ('reserved', 'settled')
                  AND created_at >= ?
                """,
                (principal_id, since_ts),
            ).fetchone()
        return {
            "principal_id": principal_id,
            "since": since_ts,
            "used_amount_usd": float(row["used_amount_usd"]) if row else 0.0,
            "used_tokens": int(row["total_tokens"]) if row else 0,
            "count": int(row["count"]) if row else 0,
        }

    # ---------- P11-1: 持久入队、Job 管理与原子提交 ----------

    def enqueue_job(
        self,
        *,
        task_id: str,
        priority: int = 0,
    ) -> dict[str, Any]:
        """将任务作业持久化插入 jobs 队列 (P11-1)。"""
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, task_id, status, priority, worker_id, lease_expires_at, fencing_token, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, NULL, NULL, 0, ?, ?)
                """,
                (job_id, task_id, priority, now, now),
            )
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_job_by_task_id(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(0, min(limit, 200)))
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_duration_seconds: float = 60.0,
    ) -> dict[str, Any] | None:
        """从队列中原子领取待执行 Job (P11-1)。

        支持按 priority 降序、created_at 升序领取处于 pending 状态，
        或者租约已过期的 claimed 任务（自动回收超时任务并递增 fencing_token）。
        同一锁与事务内完成，保证独占领取。
        """
        now = _now()
        lease_exp = (datetime.now(UTC) + timedelta(seconds=lease_duration_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT id, task_id, priority, fencing_token
                FROM jobs
                WHERE status = 'pending'
                   OR (status = 'claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            job_id, task_id, _priority, token = row[0], row[1], row[2], int(row[3] or 0)
            new_token = token + 1
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'claimed',
                    worker_id = ?,
                    lease_expires_at = ?,
                    fencing_token = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_exp, new_token, now, job_id),
            )
            self._conn.execute(
                "UPDATE tasks SET status = 'RUNNING' WHERE id = ? AND status = 'QUEUED'",
                (task_id,),
            )
            claimed = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(claimed) if claimed else None

    def renew_job_lease(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_duration_seconds: float = 60.0,
        fencing_token: int | None = None,
    ) -> bool:
        """续期 Job 租约心跳 (P11-1/P12)。仅当前持有 worker 且处于 claimed 状态时有效。"""
        now = _now()
        lease_exp = (datetime.now(UTC) + timedelta(seconds=lease_duration_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self._lock, self._conn:
            clause = "WHERE task_id = ? AND worker_id = ? AND status = 'claimed'"
            params: list[Any] = [lease_exp, now, task_id, worker_id]
            if fencing_token is not None:
                clause += " AND fencing_token = ?"
                params.append(fencing_token)
            cur = self._conn.execute(
                f"""
                UPDATE jobs
                SET lease_expires_at = ?,
                    updated_at = ?
                {clause}
                """,
                tuple(params),
            )
            return cur.rowcount > 0

    def complete_job(
        self,
        *,
        task_id: str,
        worker_id: str | None = None,
        fencing_token: int | None = None,
        status: str = "completed",
    ) -> bool:
        """完成/关闭 Job (P11-1/P12)。清除租约。"""
        now = _now()
        with self._lock, self._conn:
            clause = "WHERE task_id = ?"
            params: list[Any] = [status, now, task_id]
            if worker_id is not None:
                clause += " AND (worker_id = ? OR worker_id IS NULL)"
                params.append(worker_id)
            if fencing_token is not None:
                clause += " AND (fencing_token = ? OR fencing_token = 0)"
                params.append(fencing_token)
            cur = self._conn.execute(
                f"""
                UPDATE jobs
                SET status = ?,
                    lease_expires_at = NULL,
                    updated_at = ?
                {clause}
                """,
                tuple(params),
            )
            return cur.rowcount > 0

    def submit_task_atomic(
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
        owner: str = "legacy-unowned",
        principal_id: str = "legacy-unowned",
        spec_json: str,
        schema_version: int,
        amount_usd: float = 0.0,
        tokens: int = 0,
        daily_budget_usd: float = 0.0,
        daily_token_budget: int = 0,
        priority: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """原子提交任务、契约、额度预留与持久作业队列 (P11-1)。

        在单个 SQLite 事务中完成全部 4 步:
        1. 校验当日额度并预留 quota_reservations
        2. 插入 tasks (受 uq_tasks_active_principal_idem 幂等约束)
        3. 写入 task_spec_json / hash
        4. 插入 jobs 待执行队列
        返回 (task_dict, job_dict)。
        任意一步失败（超额、唯一键冲突、SQL 错误）整个事务回滚，零残留。
        """
        start_of_today = datetime.now(UTC).strftime("%Y-%m-%d 00:00:00")
        reservation_id = f"res-{uuid.uuid4().hex[:12]}"
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = _now()

        with self._lock, self._conn:
            # 1. 额度检查
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(reserved_amount), 0.0) AS total_amount,
                    COALESCE(SUM(reserved_tokens), 0) AS total_tokens
                FROM quota_reservations
                WHERE principal_id = ?
                  AND status IN ('reserved', 'settled')
                  AND created_at >= ?
                """,
                (principal_id, start_of_today),
            ).fetchone()
            used_amount = float(row[0]) if row else 0.0
            used_tokens = int(row[1]) if row else 0

            if daily_budget_usd > 0.0 and (used_amount + amount_usd) > daily_budget_usd:
                from app.errors import QuotaExceededError

                raise QuotaExceededError(
                    f"principal '{principal_id}' daily budget exceeded: "
                    f"requested {amount_usd:.4f} USD, currently used {used_amount:.4f} USD, "
                    f"daily limit {daily_budget_usd:.4f} USD"
                )

            if daily_token_budget > 0 and (used_tokens + tokens) > daily_token_budget:
                from app.errors import QuotaExceededError

                raise QuotaExceededError(
                    f"principal '{principal_id}' daily token budget exceeded: "
                    f"requested {tokens} tokens, currently used {used_tokens} tokens, "
                    f"daily limit {daily_token_budget} tokens"
                )

            # 2. 插入额度预留
            self._conn.execute(
                """
                INSERT INTO quota_reservations (
                    id, task_id, principal_id, reserved_amount, reserved_tokens, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (reservation_id, task_id, principal_id, amount_usd, tokens, now),
            )

            # 3. 插入任务与契约 (受 uq_tasks_active_principal_idem 幂等唯一约束)
            try:
                self.create_task(
                    task_id=task_id,
                    idem_key=idem_key,
                    bug_id=bug_id,
                    repo_path=repo_path,
                    issue_text=issue_text,
                    max_rounds=max_rounds,
                    engine=engine,
                    model_provider=model_provider,
                    run_dir=run_dir,
                    owner=owner,
                    principal_id=principal_id,
                )
                self.set_task_spec(
                    task_id,
                    spec_json=spec_json,
                    spec_hash=idem_key,
                    schema_version=schema_version,
                )
            except sqlite3.IntegrityError as exc:
                from app.errors import PatchPilotError

                raise PatchPilotError(
                    f"active task for idem_key {idem_key[:12]} already running for principal {principal_id}"
                ) from exc

            # 4. 插入持久作业队列
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, task_id, status, priority, worker_id, lease_expires_at, fencing_token, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, NULL, NULL, 0, ?, ?)
                """,
                (job_id, task_id, priority, now, now),
            )

            t_row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            j_row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            task_dict = _task_out(t_row)
            assert task_dict is not None
            return task_dict, dict(j_row)

    def check_health(self) -> tuple[bool, int, str]:
        """检查数据库连通性及当前迁移版本 (P13)。

        返回 (is_healthy, current_version, message)。
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                ).fetchone()
                version = int(row[0]) if row else 0
                return True, version, "ok"
        except Exception as exc:
            return False, -1, str(exc)

    def get_queue_stats(self) -> dict[str, Any]:
        """获取队列统计信息 (P13)。"""
        with self._lock:
            pending_row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
            ).fetchone()
            claimed_row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'claimed'"
            ).fetchone()
            oldest_row = self._conn.execute(
                "SELECT created_at FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()

        pending_count = int(pending_row[0]) if pending_row else 0
        claimed_count = int(claimed_row[0]) if claimed_row else 0
        oldest_waiting_seconds = 0.0
        if oldest_row and oldest_row[0]:
            try:
                created_dt = datetime.fromisoformat(str(oldest_row[0]))
                oldest_waiting_seconds = max(0.0, (datetime.now(UTC) - created_dt).total_seconds())
            except Exception:
                oldest_waiting_seconds = 0.0

        return {
            "pending_count": pending_count,
            "claimed_count": claimed_count,
            "oldest_waiting_seconds": oldest_waiting_seconds,
        }

    def get_task_stats(self) -> dict[str, Any]:
        """获取任务按状态与判定统计 (P13)。"""
        with self._lock:
            status_rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
            ).fetchall()
            verdict_rows = self._conn.execute(
                "SELECT verdict, COUNT(*) FROM tasks WHERE verdict IS NOT NULL AND verdict != '' GROUP BY verdict"
            ).fetchall()

        status_counts = {str(row[0]): int(row[1]) for row in status_rows}
        verdict_counts = {str(row[0]): int(row[1]) for row in verdict_rows}
        active_statuses = {"QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_INPUT"}
        active_count = sum(cnt for st, cnt in status_counts.items() if st in active_statuses)

        return {
            "active_tasks": active_count,
            "status_counts": status_counts,
            "verdict_counts": verdict_counts,
        }

    def get_quota_stats(self) -> dict[str, Any]:
        """获取当前未结算预留配额统计 (P13)。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(reserved_amount), 0.0), COALESCE(SUM(reserved_tokens), 0)
                FROM quota_reservations
                WHERE status = 'reserved'
                """
            ).fetchone()

        reserved_usd = float(row[0]) if row else 0.0
        reserved_tokens = int(row[1]) if row else 0
        return {
            "total_reserved_usd": reserved_usd,
            "total_reserved_tokens": reserved_tokens,
        }

    def set_legal_hold(self, task_id: str, hold: bool) -> bool:
        """设置或解除任务的 Legal Hold (P14-1)。

        被置 Legal Hold 的任务禁止回收任何工件产物。
        """
        val = 1 if hold else 0
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET legal_hold = ? WHERE id = ?",
                (val, task_id),
            )
            return cur.rowcount > 0

    def get_legal_hold(self, task_id: str) -> bool:
        """查询任务是否处于 Legal Hold 留置状态 (P14-1)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT legal_hold FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return bool(row and row[0])

    def register_artifact(
        self,
        task_id: str,
        artifact_type: str,
        path: str,
        size_bytes: int,
        sha256: str,
        retention_policy: str = "forensic",
    ) -> int:
        """登记工件审计元数据 (P14-1)。"""
        now = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO artifact_records (
                    task_id, artifact_type, path, size_bytes, sha256, created_at, retention_policy, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (task_id, artifact_type, path, size_bytes, sha256, now, retention_policy),
            )
            return int(cur.lastrowid or 0)

    def list_artifacts(self, task_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        """查询任务登记的工件记录 (P14-1)。"""
        query = "SELECT * FROM artifact_records WHERE task_id = ?"
        if not include_deleted:
            query += " AND is_deleted = 0"
        with self._lock:
            rows = self._conn.execute(query, (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_artifacts_deleted(self, task_id: str, paths: list[str]) -> int:
        """标记工件记录为已回收 (P14-1)。"""
        if not paths:
            return 0
        with self._lock, self._conn:
            cur = self._conn.executemany(
                "UPDATE artifact_records SET is_deleted = 1 WHERE task_id = ? AND path = ?",
                [(task_id, p) for p in paths],
            )
            return cur.rowcount
