"""健康检查与探针 (P13: liveness 与 readiness)。

规则：
- liveness: 仅检查进程基本存活；
- readiness: 检查数据库 schema、工件盘写入与空闲空间、队列状态；
- 严禁调用付费模型或外部网络；
- 严禁在探针响应中泄露 token、密钥、repo 绝对路径或内部私密。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.repository import Repository

log = logging.getLogger(__name__)


def check_liveness() -> dict[str, Any]:
    """存活探针：证明 API 进程主事件循环正常运转。"""
    return {
        "status": "alive",
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }


def check_readiness(
    repo: Repository | None,
    runs_root: Path | str | None,
    worker_enabled: bool = True,
    min_free_disk_bytes: int = 100 * 1024 * 1024,
) -> tuple[bool, dict[str, Any]]:
    """就绪探针：检查服务是否具备承接任务的生产条件。

    返回 (is_ready, details)。
    """
    now = datetime.now(UTC).isoformat(timespec="milliseconds")

    # 1. 检查数据库连通性与迁移版本
    db_ok = False
    db_version = 0
    db_msg = "uninitialized"
    if repo is None:
        db_msg = "repository instance not provided"
    else:
        db_ok, db_version, db_msg = repo.check_health()
        if db_ok and db_version < 1:
            db_ok = False
            db_msg = f"schema version too low ({db_version})"

    # 2. 检查工件目录写探测与磁盘可用空间
    disk_ok = False
    free_bytes = 0
    disk_msg = "uninitialized"
    if runs_root is None:
        disk_msg = "runs_root not provided"
    else:
        runs_dir = Path(runs_root)
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
            probe_file = runs_dir / f".probe_readiness_{uuid.uuid4().hex[:6]}"
            probe_file.write_text("probe", encoding="utf-8")
            probe_file.unlink(missing_ok=True)

            usage = shutil.disk_usage(runs_dir)
            free_bytes = usage.free
            if free_bytes < min_free_disk_bytes:
                disk_ok = False
                disk_msg = (
                    f"insufficient disk space: free {free_bytes} bytes < "
                    f"minimum required {min_free_disk_bytes} bytes"
                )
            else:
                disk_ok = True
                disk_msg = "ok"
        except Exception as exc:
            disk_ok = False
            disk_msg = f"storage probe error: {exc}"

    # 3. 检查作业队列健康状态
    queue_ok = False
    queue_stats: dict[str, Any] = {}
    if repo is not None and db_ok:
        try:
            queue_stats = repo.get_queue_stats()
            queue_ok = True
        except Exception as exc:
            queue_ok = False
            log.warning("failed to query queue stats during readiness probe: %s", exc)
    elif not db_ok:
        queue_ok = False

    is_ready = bool(db_ok and disk_ok and queue_ok)

    details: dict[str, Any] = {
        "status": "ready" if is_ready else "unready",
        "timestamp": now,
        "database": {
            "healthy": db_ok,
            "schema_version": db_version,
            "detail": db_msg,
        },
        "storage": {
            "healthy": disk_ok,
            "free_bytes": free_bytes,
            "min_required_bytes": min_free_disk_bytes,
            "detail": disk_msg,
        },
        "queue": {
            "healthy": queue_ok,
            "pending": queue_stats.get("pending_count", 0),
            "claimed": queue_stats.get("claimed_count", 0),
            "oldest_waiting_seconds": queue_stats.get("oldest_waiting_seconds", 0.0),
        },
        "worker": {
            "enabled": worker_enabled,
        },
    }

    return is_ready, details
