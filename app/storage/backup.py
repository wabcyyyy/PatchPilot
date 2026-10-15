"""SQLite 与工件一致性热备份及恢复模块 (P14-2 / P14-3)。

规则：
1. SQLite WAL 一致性：使用 stdlib sqlite3.Connection.backup API 制作一致性热备份，
   不直接通过文件复制，防止 WAL 丢失或断页；
2. 清单对账 (Manifest Reconciliation)：备份包包含 backup_manifest.json，
   记录文件相对路径、字节数、SHA-256 哈希、TaskSpec/候选/账本快照；
3. 空目录安全恢复：恢复前必须校验清单哈希与 PRAGMA integrity_check，
   默认仅恢复到空目录或独立测试实例，绝不覆盖原生产目录。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.api.recycle import FORENSIC_ENTRIES
from app.storage.repository import Repository

log = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_backup(
    repo: Repository,
    backup_root: Path | str,
    runs_root: Path | str | None = None,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """制作一致性热备份包 (P14-2)。"""
    t0 = time.monotonic()
    bid = backup_id or f"bak-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    backup_dir = Path(backup_root) / bid
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. SQLite WAL 一致热备
    db_dst = backup_dir / "patchpilot.sqlite3"
    with repo._lock:
        dst_conn = sqlite3.connect(str(db_dst))
        repo._conn.backup(dst_conn)
        dst_conn.close()

    files_manifest: dict[str, dict[str, Any]] = {
        "patchpilot.sqlite3": {
            "size_bytes": db_dst.stat().st_size,
            "sha256": _file_sha256(db_dst),
        }
    }

    # 2. 收集各任务的取证工件 (Forensic Artifacts)
    runs_target_dir = backup_dir / "runs"
    tasks_summary: list[dict[str, Any]] = []

    with repo._lock:
        task_rows = repo._conn.execute("SELECT * FROM tasks").fetchall()

    for row in task_rows:
        t_dict = dict(row)
        tid = t_dict.get("id") or t_dict.get("task_id")
        tasks_summary.append(
            {
                "task_id": tid,
                "status": t_dict.get("status"),
                "owner": t_dict.get("owner"),
                "verdict": t_dict.get("verdict"),
                "legal_hold": bool(t_dict.get("legal_hold", 0)),
            }
        )

        run_dir_str = t_dict.get("run_dir")
        if not run_dir_str:
            continue
        src_run_dir = Path(run_dir_str)
        if not src_run_dir.exists():
            continue

        dst_run_dir = runs_target_dir / tid
        dst_run_dir.mkdir(parents=True, exist_ok=True)

        # 复制取证文件
        for entry_name in FORENSIC_ENTRIES:
            src_file = src_run_dir / entry_name
            if src_file.is_file():
                dst_file = dst_run_dir / entry_name
                shutil.copy2(src_file, dst_file)
                rel_path = f"runs/{tid}/{entry_name}"
                files_manifest[rel_path] = {
                    "size_bytes": dst_file.stat().st_size,
                    "sha256": _file_sha256(dst_file),
                }

        # 复制 reports/ 目录下的 xml
        src_reports = src_run_dir / "reports"
        if src_reports.is_dir():
            dst_reports = dst_run_dir / "reports"
            for xml_file in src_reports.glob("*.xml"):
                dst_reports.mkdir(parents=True, exist_ok=True)
                dst_xml = dst_reports / xml_file.name
                shutil.copy2(xml_file, dst_xml)
                rel_path = f"runs/{tid}/reports/{xml_file.name}"
                files_manifest[rel_path] = {
                    "size_bytes": dst_xml.stat().st_size,
                    "sha256": _file_sha256(dst_xml),
                }

    # 3. 统计配额与版本
    quota_summary = repo.get_quota_stats() if hasattr(repo, "get_quota_stats") else {}
    _, schema_ver, _ = repo.check_health() if hasattr(repo, "check_health") else (True, 0, "")

    duration = time.monotonic() - t0
    manifest: dict[str, Any] = {
        "backup_id": bid,
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "schema_version": schema_ver,
        "duration_seconds": round(duration, 4),
        "files": files_manifest,
        "tasks_count": len(tasks_summary),
        "tasks_summary": tasks_summary,
        "quota_summary": quota_summary,
    }

    manifest_path = backup_dir / "backup_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("backup completed: id=%s files=%d duration=%.2fs", bid, len(files_manifest), duration)
    return manifest


def verify_backup(backup_dir: Path | str) -> tuple[bool, list[str]]:
    """校验备份包完整性、哈希与 SQLite 页面状态 (P14-2)。"""
    bdir = Path(backup_dir)
    manifest_file = bdir / "backup_manifest.json"
    if not manifest_file.is_file():
        return False, ["backup_manifest.json missing"]

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"failed to parse backup_manifest.json: {exc}"]

    errors: list[str] = []
    files = manifest.get("files", {})
    for rel_path, meta in files.items():
        fpath = bdir / rel_path
        if not fpath.is_file():
            errors.append(f"missing file in backup: {rel_path}")
            continue
        actual_size = fpath.stat().st_size
        if actual_size != meta.get("size_bytes"):
            errors.append(
                f"file size mismatch for {rel_path}: expected {meta.get('size_bytes')}, got {actual_size}"
            )
        actual_sha = _file_sha256(fpath)
        if actual_sha != meta.get("sha256"):
            errors.append(
                f"sha256 mismatch for {rel_path}: expected {meta.get('sha256')}, got {actual_sha}"
            )

    # 校验 SQLite integrity
    db_file = bdir / "patchpilot.sqlite3"
    if db_file.is_file():
        try:
            conn = sqlite3.connect(str(db_file))
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0].lower() != "ok":
                errors.append(f"sqlite integrity check failed: {check}")
            t_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if t_count != manifest.get("tasks_count", t_count):
                errors.append(
                    f"tasks count mismatch: manifest {manifest.get('tasks_count')} vs db {t_count}"
                )
            conn.close()
        except Exception as exc:
            errors.append(f"sqlite check error: {exc}")

    return len(errors) == 0, errors


def restore_backup(
    backup_dir: Path | str,
    target_dir: Path | str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """空目录安全恢复 (P14-3)。恢复前强制校验，严禁静默覆盖非空目录。"""
    t0 = time.monotonic()
    bdir = Path(backup_dir)
    tdir = Path(target_dir)

    is_valid, errors = verify_backup(bdir)
    if not is_valid:
        raise ValueError(f"cannot restore corrupted backup: {'; '.join(errors)}")

    if tdir.exists():
        entries = list(tdir.iterdir())
        if entries and not overwrite:
            raise ValueError(
                f"target restore directory '{tdir}' is not empty ({len(entries)} items). "
                "Refusing to overwrite without explicit overwrite=True."
            )
    else:
        tdir.mkdir(parents=True, exist_ok=True)

    # 1. 恢复数据库
    src_db = bdir / "patchpilot.sqlite3"
    dst_db = tdir / "patchpilot.sqlite3"
    shutil.copy2(src_db, dst_db)

    # 2. 恢复工件目录
    src_runs = bdir / "runs"
    dst_runs = tdir / "runs"
    if src_runs.is_dir():
        if dst_runs.exists() and overwrite:
            shutil.rmtree(dst_runs)
        shutil.copytree(src_runs, dst_runs, dirs_exist_ok=True)

    # 3. 校验恢复后数据库可用性与数据一致性
    conn = sqlite3.connect(str(dst_db))
    t_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()

    duration = time.monotonic() - t0
    log.info("restore completed: target=%s tasks=%d duration=%.2fs", tdir, t_count, duration)
    return {
        "status": "success",
        "target_dir": str(tdir),
        "tasks_restored": t_count,
        "duration_seconds": round(duration, 4),
    }
