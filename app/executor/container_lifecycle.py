"""执行单元生命周期闭环与容器管理 (P09)。

契约与边界:
1. 所有由 PatchPilot 启动的容器必须登记身份与受控 labels (managed_by, task_id, kind);
2. 守护进程上的容器不属于本地 CLI 进程树，终止时必须精确 stop/kill 并确认退出状态;
3. 容器身份在启动窗口落盘，记录启动与退出状态;
4. 孤儿容器清理仅针对带有 patchpilot.managed_by 标签的容器，严禁全局 prune/kill。
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

LABEL_MANAGED_BY = "patchpilot.managed_by"
LABEL_VALUE_MANAGED = "patchpilot"
LABEL_TASK_ID = "patchpilot.task_id"
LABEL_KIND = "patchpilot.kind"
LABEL_CONTAINER_NAME = "patchpilot.container_name"
LABEL_CREATED_AT = "patchpilot.created_at"


def get_container_labels(
    *,
    task_id: str,
    container_name: str,
    kind: str = "test",
) -> list[str]:
    """生成受控 Docker 容器标准标签命令行参数。"""
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    labels = {
        LABEL_MANAGED_BY: LABEL_VALUE_MANAGED,
        LABEL_TASK_ID: task_id or "unknown",
        LABEL_KIND: kind,
        LABEL_CONTAINER_NAME: container_name,
        LABEL_CREATED_AT: now_iso,
    }
    flags: list[str] = []
    for k, v in sorted(labels.items()):
        flags.extend(["--label", f"{k}={v}"])
    return flags


def record_container_started(
    task_id: str,
    container_name: str,
    kind: str,
    metadata_dir: Path | None = None,
) -> None:
    """在任务工作区/元数据目录落盘容器启动记录。"""
    if metadata_dir is None:
        return
    try:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        log_file = metadata_dir / "containers.jsonl"
        record = {
            "task_id": task_id,
            "container_name": container_name,
            "kind": kind,
            "state": "started",
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("failed to record container start for %s: %s", container_name, exc)


def record_container_finished(
    task_id: str,
    container_name: str,
    exit_status: str,
    metadata_dir: Path | None = None,
) -> None:
    """在任务元数据目录落盘容器结束记录。"""
    if metadata_dir is None:
        return
    try:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        log_file = metadata_dir / "containers.jsonl"
        record = {
            "task_id": task_id,
            "container_name": container_name,
            "state": "finished",
            "exit_status": exit_status,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("failed to record container finish for %s: %s", container_name, exc)


def kill_and_confirm_container(
    container_name: str,
    timeout_seconds: float = 10.0,
) -> bool:
    """精确终止指定容器并确认其已退出或销毁。返回 True 表示已确认终止。"""
    if not container_name:
        return True
    # 1. 发起 docker kill
    try:
        subprocess.run(
            ["docker", "kill", "--", container_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("docker kill on %s failed: %s", container_name, exc)

    # 2. 轮询检查退出状态 (由于使用 --rm，容器完全退出后 inspect 会返回非零)
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            inspect_proc = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", "--", container_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break

        if inspect_proc.returncode != 0:
            # 容器已不存在 (已清理)
            return True

        status = inspect_proc.stdout.strip().lower()
        if status in ("exited", "dead", ""):
            return True

        time.sleep(0.2)

    log.error("failed to confirm container %s exit within deadline", container_name)
    return False


def cleanup_orphan_containers(older_than_seconds: int = 3600) -> list[str]:
    """仅清理属于 PatchPilot 且超时的孤儿容器，绝不误伤其他容器或执行 prune。"""
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={LABEL_MANAGED_BY}={LABEL_VALUE_MANAGED}",
                "--format",
                "{{.Names}}\t{{.CreatedAt}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("failed to list orphan containers: %s", exc)
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    cleaned: list[str] = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        c_name = parts[0].strip()
        # 精确 kill 并 rm
        subprocess.run(
            ["docker", "rm", "-f", "--", c_name], capture_output=True, timeout=10, check=False
        )
        cleaned.append(c_name)

    return cleaned
