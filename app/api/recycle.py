"""终态产物回收(P3-12):取证集永久,可弃集回收。

审计 R3-Q3:tracker「文件副本永久保留」承诺与无界增长(满负荷外推
3-10GB/日)在数学上不可同时成立——改承诺,不是瞒事实。回收策略(如实声明):

- 取证集(永久保留):report.json、diff.patch、trajectory.jsonl、
  reports/*.xml(junit)——复盘与审计的证据;
- 可弃集(仅 FINISHED 回收):工作区副本 workspace/ 与 checkpoints.sqlite
  ——补丁已在 diff.patch,成功任务的工作区是纯冗余(实测占单任务足迹 80%);
  失败/取消/NEEDS_REVIEW 任务保留现场(语义是"需要人看",回收让复盘失明);
- 启动 Grace 扫描:崩溃/重启场景下 _execute 收尾没跑到的 FINISHED 任务,
  由服务启动时补收——只动 finished_at 早于 grace 截止的行,刚结束的留给
  人看;运行期在 service._execute 终态回写后即时回收(仅 FINISHED 路径)。
  **语义是最终一致,不是瞬时**(实测窗口:终态回写先于回收完成,观察者可能
  在极短窗口内读到 FINISHED 而可弃集尚在;轮询方不可假设两者原子);
- Windows 占用两类:工作区里的 git objects 是只读文件(WinError 5,删除前
  整树去只读);报告/轮询方短暂持有时 rmtree 抛 sharing violation
  (WinError 32),短退避重试数次后放弃本次(下次启动扫描再收)。
"""

from __future__ import annotations

import logging
import shutil
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.storage.repository import Repository

log = logging.getLogger(__name__)

# 可弃集条目(run_dir 相对路径);其余一切默认取证、保留
DISPOSABLE_ENTRIES: tuple[str, ...] = ("workspace", "checkpoints.sqlite")


def _make_writable(target: Path) -> None:
    """整树去只读。工作区是物化 git 仓库,git objects 落盘为只读文件(444),
    Windows 的 rmtree 对它们直接 WinError 5——删除前先清属性。"""
    if not target.is_dir():
        return
    for path in target.rglob("*"):
        try:
            mode = path.stat().st_mode
            if not mode & stat.S_IWRITE:
                path.chmod(mode | stat.S_IWRITE)
        except OSError:  # 单个文件去只读失败不阻断整树
            continue
    try:
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IWRITE)
    except OSError:
        pass


def _remove_path(target: Path, attempts: int = 3, backoff_seconds: float = 0.3) -> bool:
    """删除目录或文件;最终失败返回 False(不抛,不阻断任务收尾)。

    两类 Windows 占用分而治之:git 只读对象(WinError 5)在删除前整树去只读;
    句柄被短暂持有(WinError 32,sharing violation)靠退避重试。
    """
    for attempt in range(attempts):
        try:
            if target.is_dir() and not target.is_symlink():
                _make_writable(target)
                shutil.rmtree(target)
            else:
                target.unlink()  # checkpoints.sqlite 是文件,rmtree 会 NotADirectoryError
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


def recycle_run_dir(run_dir: Path) -> int:
    """删除一个 run 目录下的可弃集条目;返回成功回收的条目数。取证文件不动。"""
    removed = 0
    for name in DISPOSABLE_ENTRIES:
        target = Path(run_dir) / name
        if not target.exists():
            continue
        if _remove_path(target):
            removed += 1
    return removed


def recycle_finished_tasks(repo: Repository, grace_seconds: int) -> list[str]:
    """启动 Grace 扫描:回收 finished_at 早于 grace 截止的 FINISHED 任务的可弃集。

    只在服务启动时调用;运行期回收挂 service._execute finally(即时)。
    返回本次实际回收的 task_id 列表。
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat(
        timespec="milliseconds"
    )
    recycled: list[str] = []
    for task in repo.list_tasks_finished_before("FINISHED", cutoff):
        run_dir = task.get("run_dir")
        if not run_dir or not Path(run_dir).exists():
            continue
        if recycle_run_dir(Path(run_dir)):
            recycled.append(task["task_id"])
    if recycled:
        log.info("startup recycle: reclaimed disposable artifacts for %d task(s)", len(recycled))
    return recycled
