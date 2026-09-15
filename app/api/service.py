"""任务服务:后台执行、幂等、状态流转与落库。

设计:
- 幂等键 = bug_id + engine + model(同键任务未到终态时直接返回原任务);
- 锁:Redis(可用时)或进程内兜底,防止同键任务并发执行;
- 崩溃恢复:服务启动时把 RUNNING/QUEUED 僵尸任务标记 NEEDS_REVIEW;
- cancel:标记 CANCELLED(执行线程不中断,终态回写时让位于 CANCELLED),保留现场。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.errors import PatchPilotError, TaskError
from app.evals.bugset import BUGS_ROOT, load_bug, load_replay_script
from app.storage.locks import BaseLock, build_lock
from app.storage.repository import Repository

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TaskService:
    def __init__(
        self,
        *,
        repo: Repository,
        runs_root: Path | None = None,
        bugs_root: Path | str = BUGS_ROOT,
        redis_url: str = "",
        lock: BaseLock | None = None,
        max_workers: int = 2,
    ) -> None:
        self.repo = repo
        self.runs_root = (runs_root or get_settings().runs_root).resolve()
        self.bugs_root = Path(bugs_root)
        self.lock = lock or build_lock(redis_url or get_settings().redis_url)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="patchpilot")

    # ---------- 创建 ----------

    def create_task(
        self,
        *,
        bug_id: str,
        engine: str = "graph",
        model: str = "fake",
        max_rounds: int | None = None,
    ) -> dict[str, Any]:
        bug = load_bug(bug_id, self.bugs_root)  # TaskError → 404/422 由路由层转
        idem_key = hashlib.sha256(f"{bug.id}|{engine}|{model}".encode()).hexdigest()

        existing = self.repo.find_by_idem_key(idem_key)
        if existing and existing["status"] not in TERMINAL_STATUSES:
            return existing  # 幂等:同键任务仍在途

        lock_key = f"task:{idem_key}"
        if not self.lock.acquire(lock_key, ttl_seconds=get_settings().task_timeout_seconds + 60):
            # 锁被占但库里查不到(极小窗口):按幂等冲突处理
            raise PatchPilotError(f"task for {bug.id} is already running")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        task_id = f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
        run_dir = self.runs_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.repo.create_task(
            task_id=task_id,
            idem_key=idem_key,
            bug_id=bug.id,
            repo_path=str(bug.repo_dir),
            issue_text=bug.issue_text[:500],
            max_rounds=max_rounds or bug.max_rounds,
            engine=engine,
            model_provider=model,
            run_dir=str(run_dir),
        )
        self.repo.update_task_status(task_id, "RUNNING")
        self._pool.submit(self._execute, task_id, bug, engine, model, run_dir, lock_key)
        return self.repo.get_task(task_id)  # type: ignore[return-value]

    # ---------- 执行 ----------

    def _execute(
        self, task_id: str, bug, engine: str, model_name: str, run_dir: Path, lock_key: str
    ) -> None:
        try:
            settings = get_settings()
            script = None
            if model_name == "fake":
                script = load_replay_script(bug, kind=engine)
            from app.llm.openai_client import build_model

            model = build_model(model_name, settings, script=script)

            if engine == "graph":
                from app.graph.runner import run_task_graph

                result = run_task_graph(
                    bug, model, runs_root=self.runs_root, task_id=task_id, run_dir=run_dir
                )
            else:
                from app.evals.driver import run_task

                result = run_task(
                    bug,
                    model,
                    runs_root=self.runs_root,
                    engine=engine,
                    task_id=task_id,
                    run_dir=run_dir,
                )

            # 终态回写(CANCELLED 不被覆盖)
            current = self.repo.get_task(task_id) or {}
            if current.get("status") != "CANCELLED":
                self.repo.update_task_status(task_id, result.status, result.verdict)
            self._persist_artifacts(task_id, result, run_dir)
        except TaskError as exc:
            self.repo.update_task_status(task_id, "INVALID_TASK", "failed")
            log.error("task %s invalid: %s", task_id, exc)
        except Exception as exc:  # noqa: BLE001
            current = self.repo.get_task(task_id) or {}
            if current.get("status") != "CANCELLED":
                self.repo.update_task_status(task_id, "NEEDS_REVIEW", "needs_review")
            log.exception("task %s crashed", task_id)
            _ = exc
        finally:
            self.lock.release(lock_key)

    def _persist_artifacts(self, task_id: str, result: Any, run_dir: Path) -> None:
        """轨迹 JSONL → 入库;评测汇总 → evaluations 表。"""
        traj_path = run_dir / "trajectory.jsonl"
        if traj_path.exists():
            events = [
                json.loads(line) for line in traj_path.read_text(encoding="utf-8").splitlines()
            ]
            if events:
                self.repo.insert_events(task_id, events)
        try:
            report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.repo.insert_patch(
            task_id=task_id,
            round_no=report.get("rounds", 1),
            diff_path=str(Path(result.run_dir) / "diff.patch"),
            changed_files=report.get("changed_files", []),
            gate_result="passed"
            if not report.get("gate_violations")
            else "; ".join(report["gate_violations"])[:500],
            applied=bool(report.get("changed_files")),
        )
        self.repo.upsert_evaluation(
            task_id=task_id,
            bug_id=report.get("bug_id"),
            localized=int(bool(report.get("changed_files"))),
            patch_applied=int(bool(report.get("changed_files"))),
            final_resolved=int(report.get("verdict") == "resolved"),
            regression_introduced=int(
                bool(report.get("verify_failed_ok"))
                and not report.get("verify_regression_ok", True)
            ),
            security_blocked=int(bool(report.get("gate_violations"))),
            rounds=report.get("rounds"),
            tokens=report.get("tokens_used"),
            duration_ms=report.get("duration_ms"),
        )

    # ---------- 查询与控制 ----------

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.repo.get_task(task_id)

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repo.list_tasks(limit)

    def trajectory(self, task_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.repo.get_trajectory(task_id, limit, offset)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise TaskError(f"task not found: {task_id}")
        if task["status"] in TERMINAL_STATUSES:
            raise PatchPilotError(f"task {task_id} already finished ({task['status']})")
        self.repo.update_task_status(task_id, "CANCELLED")
        return self.repo.get_task(task_id)  # type: ignore[return-value]

    def recover_stale(self) -> int:
        return self.repo.recover_stale_running()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
