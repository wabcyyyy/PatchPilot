"""任务服务:后台执行、幂等、状态流转与落库。

设计:
- 幂等键 = bug_id + engine + model(同键任务未到终态时直接返回原任务);
- 锁:Redis(可用时)或进程内兜底,防止同键任务并发执行;
- 崩溃恢复:服务启动时把 RUNNING/QUEUED 僵尸任务标记 NEEDS_REVIEW;
- cancel:先置 CANCELLED,再经 CancelRegistry 通知执行线程在 turn 边界协作式中断
  (正在跑的一次 pytest/LLM 调用先完成),终态回写时让位于 CANCELLED,保留现场。
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

from app.api.cancellation import CancelRegistry
from app.config import get_settings
from app.errors import PatchPilotError, TaskError
from app.evals.bugset import BUGS_ROOT, build_custom_bug, load_bug, load_replay_script
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
        self._cancels = CancelRegistry()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="patchpilot")

    # ---------- 创建 ----------

    def create_task(
        self,
        *,
        bug_id: str | None = None,
        engine: str = "graph",
        model: str = "fake",
        max_rounds: int | None = None,
        repo_path: str | None = None,
        issue_text: str | None = None,
        failed_tests: list[str] | None = None,
        regression_tests: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        replay_script: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if repo_path is not None:
            resolved = Path(repo_path).resolve()
            if not resolved.is_dir():
                raise TaskError(f"repo_path not found or not a directory: {repo_path}")
            if model == "fake" and not replay_script:
                raise TaskError(
                    "custom repo task with model='fake' requires a replay_script;"
                    " provide replay_script or use model='openai'"
                )
            bug = build_custom_bug(
                repo_path=resolved,
                issue_text=issue_text or "",
                failed_tests=failed_tests or [],
                regression_tests=regression_tests or [],
                allowed_paths=allowed_paths,
            )
        else:
            bug = load_bug(bug_id, self.bugs_root)  # TaskError → 404/422 由路由层转
        if model == "openai":
            # 前置校验:总开关未开/凭据缺失时同步失败,不建任务、不拿锁、不烧钱
            from app.llm.openai_client import build_model

            build_model(model, get_settings())
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
        # 提交前先注册取消事件,保证 create 返回后的任何 cancel 都不会丢失
        cancel_event = self._cancels.register(task_id)
        self._pool.submit(
            self._execute,
            task_id,
            bug,
            engine,
            model,
            run_dir,
            lock_key,
            cancel_event,
            replay_script,
        )
        return self.repo.get_task(task_id)  # type: ignore[return-value]

    # ---------- 执行 ----------

    def _execute(
        self,
        task_id: str,
        bug,
        engine: str,
        model_name: str,
        run_dir: Path,
        lock_key: str,
        cancel_event,
        replay_script: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            settings = get_settings()
            script = None
            if model_name == "fake":
                # 自定义任务的回放脚本来自请求内存对象;正式题从 bugs/ 目录加载
                script = replay_script if replay_script else load_replay_script(bug, kind=engine)
            from app.llm.openai_client import build_model

            model = build_model(model_name, settings, script=script)
            # 真实模型名:openai 时取 Settings.llm_model,供成本核算(fake 为空 → 成本恒 n/a)
            real_model_name = settings.llm_model if model_name == "openai" else ""

            if engine == "graph":
                from app.graph.runner import run_task_graph

                result = run_task_graph(
                    bug,
                    model,
                    runs_root=self.runs_root,
                    task_id=task_id,
                    run_dir=run_dir,
                    model_name=real_model_name,
                    cancel_event=cancel_event,
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
                    model_name=real_model_name,
                    cancel_event=cancel_event,
                )

            # 先落产物,再翻终态:轮询方见到终态时轨迹/报告必然已可查;
            # CANCELLED 保护放在终态写入前的最后一刻,不让自然完成覆盖取消
            self._persist_artifacts(task_id, result, run_dir)
            current = self.repo.get_task(task_id) or {}
            if current.get("status") != "CANCELLED":
                self.repo.update_task_status(task_id, result.status, result.verdict)
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
            self._cancels.unregister(task_id)
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
            cost_usd=report.get("cost_usd"),
            tokens_prompt=report.get("tokens_prompt"),
            tokens_completion=report.get("tokens_completion"),
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
        # 状态落库后通知执行线程;中断在下个 turn 边界生效
        self._cancels.request_cancel(task_id)
        return self.repo.get_task(task_id)  # type: ignore[return-value]

    def recover_stale(self) -> int:
        return self.repo.recover_stale_running()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
