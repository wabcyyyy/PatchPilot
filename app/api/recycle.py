"""终态产物回收与工件生命周期 (P3-12 / P14-1)。

审计 R3-Q3: tracker「文件副本永久保留」承诺与无界增长在数学上不可同时成立。
回收策略：
- 取证集 (永久保留): report.json、diff.patch、trajectory.jsonl、reports/*.xml (junit) 等；
- 可弃集 (仅 FINISHED 且未置 legal_hold 回收): workspace/、checkpoints.sqlite、source_snapshot/；
- 失败现场处置: 失败/取消任务保留现场供复盘；
- 法定留置 (legal_hold): 无论终态与时间，置 hold 任务一律禁止回收；
- 软链接与越界防御: 严格校验 runs_root 子目录绝对路径，禁止跟踪软链接；
- 活租约保护: claimed 状态且租约未超期的作业禁止回收；
- Dry-run 支持: 生成可回收清单与释放字节预测，支持安全审查后再执行。
"""

from __future__ import annotations

import logging
import shutil
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.storage.repository import TERMINAL_STATUSES, Repository

log = logging.getLogger(__name__)

# 可弃集条目 (run_dir 相对路径)
DISPOSABLE_ENTRIES: tuple[str, ...] = ("workspace", "checkpoints.sqlite", "source_snapshot")

# 取证集条目 (严禁回收)
FORENSIC_ENTRIES: tuple[str, ...] = (
    "task_spec.json",
    "manifest.json",
    "report.json",
    "diff.patch",
    "trajectory.jsonl",
    "acceptance_manifest.json",
)


def _make_writable(target: Path) -> None:
    """整树去只读。处理 Windows 下 git 只读对象。"""
    if not target.is_dir():
        return
    for path in target.rglob("*"):
        try:
            mode = path.stat().st_mode
            if not mode & stat.S_IWRITE:
                path.chmod(mode | stat.S_IWRITE)
        except OSError:
            continue
    try:
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IWRITE)
    except OSError:
        pass


def _remove_path(target: Path, attempts: int = 3, backoff_seconds: float = 0.3) -> bool:
    """删除目录或文件；最终失败返回 False (不抛异常)。"""
    for attempt in range(attempts):
        try:
            if target.is_symlink():
                target.unlink()
                return True
            if target.is_dir():
                _make_writable(target)
                shutil.rmtree(target)
            else:
                target.unlink()
            return True
        except PermissionError:
            if attempt == attempts - 1:
                log.warning("recycle: giving up on %s after %d attempts", target, attempts)
                return False
            time.sleep(backoff_seconds * (attempt + 1))
        except OSError:
            log.exception("recycle: failed to remove %s", target)
            return False
    return False


def is_safe_run_dir(run_dir: Path | str, runs_root: Path | str | None = None) -> bool:
    """核对绝对路径、不追软链、不全局清目录 (P14-1)。"""
    try:
        p = Path(run_dir)
        if p.is_symlink():
            return False
        resolved = p.resolve()
        if not resolved.is_dir() or len(resolved.parts) < 2:
            return False
        if runs_root is not None:
            root_res = Path(runs_root).resolve()
            if resolved == root_res or not resolved.is_relative_to(root_res):
                return False
        return True
    except Exception:
        return False


def _safe_remove_entry(
    target: Path, attempts: int = 3, backoff_seconds: float = 0.3
) -> tuple[bool, int]:
    """安全回收单个可弃项，统计释放字节。遇到软链接直接 unlink 自身，不顺藤删除。"""
    if not target.exists() and not target.is_symlink():
        return False, 0
    if target.is_symlink():
        try:
            target.unlink()
            return True, 0
        except OSError:
            return False, 0

    reclaimed_bytes = 0
    try:
        if target.is_file():
            reclaimed_bytes = target.stat().st_size
        elif target.is_dir():
            reclaimed_bytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    except Exception:
        reclaimed_bytes = 0

    ok = _remove_path(target, attempts=attempts, backoff_seconds=backoff_seconds)
    return ok, (reclaimed_bytes if ok else 0)


def can_recycle_task(
    task_id: str,
    repo: Repository,
    grace_seconds: int = 0,
) -> tuple[bool, str]:
    """前置安全核对：终态、无活租约、未置留置、过 grace 窗口 (P14-1)。"""
    task = repo.get_task(task_id)
    if not task:
        return False, "task_not_found"

    status = task.get("status", "")
    if status not in TERMINAL_STATUSES:
        return False, f"task_not_terminal_{status}"

    if status != "FINISHED":
        return False, f"status_{status}_preserves_scene"

    if hasattr(repo, "get_legal_hold") and repo.get_legal_hold(task_id):
        return False, "legal_hold_active"

    # 核对活租约 (jobs 表)
    with repo._lock:
        j_row = repo._conn.execute(
            "SELECT status, lease_expires_at FROM jobs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if j_row:
            j_status = j_row[0]
            lease_exp = j_row[1]
            now_iso = datetime.now(UTC).isoformat(timespec="milliseconds")
            if j_status == "claimed" and lease_exp and lease_exp > now_iso:
                return False, "active_job_lease"

    if grace_seconds > 0:
        finished_at = task.get("finished_at")
        if finished_at:
            try:
                cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat(
                    timespec="milliseconds"
                )
                if finished_at > cutoff:
                    return False, "within_grace_period"
            except Exception:
                pass

    return True, "eligible"


def plan_recycle_task(
    task_id: str,
    repo: Repository,
    runs_root: Path | str | None = None,
    grace_seconds: int = 0,
) -> dict[str, Any]:
    """生成单任务的可回收性预测与清单 (dry-run)。"""
    task = repo.get_task(task_id) or {}
    run_dir_str = task.get("run_dir", "")
    run_dir = Path(run_dir_str) if run_dir_str else None

    can_recycle, reason = can_recycle_task(task_id, repo, grace_seconds=grace_seconds)
    if can_recycle and (not run_dir or not is_safe_run_dir(run_dir, runs_root)):
        can_recycle = False
        reason = "unsafe_or_missing_run_dir"

    disposable_found: list[str] = []
    reclaimable_bytes = 0
    if run_dir and run_dir.exists():
        for name in DISPOSABLE_ENTRIES:
            entry = run_dir / name
            if entry.exists() or entry.is_symlink():
                disposable_found.append(name)
                try:
                    if entry.is_file():
                        reclaimable_bytes += entry.stat().st_size
                    elif entry.is_dir() and not entry.is_symlink():
                        reclaimable_bytes += sum(
                            f.stat().st_size for f in entry.rglob("*") if f.is_file()
                        )
                except Exception:
                    pass

    return {
        "task_id": task_id,
        "run_dir": run_dir_str,
        "status": task.get("status", ""),
        "can_recycle": can_recycle,
        "reason": reason,
        "disposable_entries": disposable_found,
        "reclaimable_bytes": reclaimable_bytes,
    }


def plan_recycle_all(
    repo: Repository,
    grace_seconds: int = 300,
    runs_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """批量生成所有终态任务的回收 dry-run 清单。"""
    cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat(
        timespec="milliseconds"
    )
    candidates = repo.list_tasks_finished_before("FINISHED", cutoff)
    return [
        plan_recycle_task(t["task_id"], repo, runs_root=runs_root, grace_seconds=grace_seconds)
        for t in candidates
    ]


def execute_recycle_plan(
    plan: list[dict[str, Any]],
    repo: Repository | None = None,
) -> dict[str, Any]:
    """执行已审核的回收清单，只有 can_recycle=True 的项才触发物理删除。"""
    recycled_tasks: list[str] = []
    failed_tasks: list[str] = []
    total_reclaimed_bytes = 0

    for item in plan:
        if not item.get("can_recycle"):
            continue
        task_id = item["task_id"]
        run_dir_str = item.get("run_dir", "")
        if not run_dir_str or not Path(run_dir_str).exists():
            continue
        run_dir = Path(run_dir_str)

        task_bytes = 0
        removed_count = 0
        removed_paths: list[str] = []
        for name in DISPOSABLE_ENTRIES:
            entry = run_dir / name
            if entry.exists() or entry.is_symlink():
                ok, b = _safe_remove_entry(entry)
                if ok:
                    removed_count += 1
                    task_bytes += b
                    removed_paths.append(str(entry))

        if removed_count > 0:
            recycled_tasks.append(task_id)
            total_reclaimed_bytes += task_bytes
            if repo is not None and hasattr(repo, "mark_artifacts_deleted"):
                repo.mark_artifacts_deleted(task_id, removed_paths)
        else:
            failed_tasks.append(task_id)

    return {
        "recycled_tasks": recycled_tasks,
        "failed_tasks": failed_tasks,
        "reclaimed_bytes": total_reclaimed_bytes,
    }


def recycle_run_dir(run_dir: Path, runs_root: Path | None = None) -> int:
    """删除一个 run 目录下的可弃集条目；返回成功回收的条目数。取证文件不动。"""
    if not is_safe_run_dir(run_dir, runs_root):
        log.warning("recycle_run_dir rejected unsafe path: %s", run_dir)
        return 0
    removed = 0
    for name in DISPOSABLE_ENTRIES:
        target = Path(run_dir) / name
        if not target.exists() and not target.is_symlink():
            continue
        ok, _ = _safe_remove_entry(target)
        if ok:
            removed += 1
    return removed


def recycle_finished_tasks(
    repo: Repository,
    grace_seconds: int,
    runs_root: Path | str | None = None,
    dry_run: bool = False,
) -> list[str] | list[dict[str, Any]]:
    """启动 Grace 扫描：回收 finished_at 早于 grace 截止的 FINISHED 任务。

    dry_run=True 时返回结构化预检清单，不执行删除。
    """
    plan = plan_recycle_all(repo, grace_seconds=grace_seconds, runs_root=runs_root)
    if dry_run:
        return plan

    result = execute_recycle_plan(plan, repo=repo)
    recycled = result["recycled_tasks"]
    if recycled:
        log.info(
            "startup recycle: reclaimed %d task(s), %d bytes",
            len(recycled),
            result["reclaimed_bytes"],
        )
    return recycled
