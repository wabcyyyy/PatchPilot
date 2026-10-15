"""PatchPilot 备份与恢复运维脚本 (P14-2 / P14-3)。

用法：
- 备份：python -m scripts.backup_restore backup --target ./backups
- 校验：python -m scripts.backup_restore verify --backup ./backups/bak-xxx
- 恢复：python -m scripts.backup_restore restore --backup ./backups/bak-xxx --target ./restore_target
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import get_settings
from app.storage.backup import create_backup, restore_backup, verify_backup
from app.storage.repository import Repository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot 备份与恢复运维工具")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # backup
    p_backup = subparsers.add_parser("backup", help="制作热备份包")
    p_backup.add_argument("--target", required=True, help="备份根目录")
    p_backup.add_argument("--db", default=None, help="源数据库路径 (默认读配置)")
    p_backup.add_argument("--runs", default=None, help="源 runs 路径 (默认读配置)")

    # verify
    p_verify = subparsers.add_parser("verify", help="校验备份包完整性")
    p_verify.add_argument("--backup", required=True, help="待校验备份目录")

    # restore
    p_restore = subparsers.add_parser("restore", help="恢复备份到指定空目录")
    p_restore.add_argument("--backup", required=True, help="备份包路径")
    p_restore.add_argument("--target", required=True, help="恢复目标空目录")
    p_restore.add_argument("--overwrite", action="store_true", help="允许覆盖非空目标目录")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.action == "backup":
        db_path = Path(args.db) if args.db else settings.db_path
        runs_path = Path(args.runs) if args.runs else settings.runs_root
        repo = Repository(db_path)
        manifest = create_backup(repo, args.target, runs_root=runs_path)
        print(f"[OK] Backup created successfully: {manifest['backup_id']}")
        print(f"     Tasks backed up: {manifest['tasks_count']}, Files: {len(manifest['files'])}")
        return 0

    if args.action == "verify":
        is_valid, errors = verify_backup(args.backup)
        if not is_valid:
            print(
                f"[FAIL] Backup verification failed with {len(errors)} error(s):", file=sys.stderr
            )
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(f"[OK] Backup '{args.backup}' integrity verified successfully.")
        return 0

    if args.action == "restore":
        try:
            result = restore_backup(args.backup, args.target, overwrite=args.overwrite)
            print(f"[OK] Restore completed to {result['target_dir']}")
            print(
                f"     Tasks restored: {result['tasks_restored']} in {result['duration_seconds']}s"
            )
            return 0
        except Exception as exc:
            print(f"[FAIL] Restore failed: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
