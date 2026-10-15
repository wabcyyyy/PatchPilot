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

        恢复要按行决定去向(有可用快照 → 重新入队续跑;没有 → 判死),所以需要的不只是
        idem_key。只读方法,不改状态——写仍集中在 `recover_stale_running`。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('RUNNING','QUEUED','WAITING_INPUT')"
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
                "SELECT idem_key FROM tasks"
                " WHERE status IN ('RUNNING','QUEUED','WAITING_INPUT')" + clause,
                params,
            ).fetchall()
            stale_keys = [r["idem_key"] for r in rows if r["idem_key"]]
            self._conn.execute(
                "UPDATE tasks SET status='NEEDS_REVIEW', finished_at=?"
                " WHERE status IN ('RUNNING','QUEUED','WAITING_INPUT')" + clause,
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
    ) -> dict[str, Any] | None:
        """结算任务配额 (P10-2)。"""
        now = _now()
        with self._lock, self._conn:
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
    ) -> dict[str, Any] | None:
        """释放未使用的预留配额 (P10-2)。"""
        now = _now()
        with self._lock, self._conn:
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
