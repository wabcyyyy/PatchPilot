"""测试一致性热备份与恢复 (P14-2 / P14-3)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.backup import create_backup, restore_backup, verify_backup
from app.storage.repository import Repository
from scripts.backup_restore import main as cli_main


def _setup_test_env(tmp_path: Path) -> tuple[Repository, Path]:
    db_file = tmp_path / "origin.sqlite3"
    runs_dir = tmp_path / "origin_runs"
    runs_dir.mkdir()
    repo = Repository(db_file)

    # 创建任务与工件
    t1_dir = runs_dir / "T-BAK1"
    (t1_dir / "workspace").mkdir(parents=True)
    (t1_dir / "workspace" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (t1_dir / "reports").mkdir()
    (t1_dir / "reports" / "test.xml").write_text("<testsuite/>", encoding="utf-8")
    (t1_dir / "report.json").write_text('{"verdict": "resolved"}', encoding="utf-8")
    (t1_dir / "diff.patch").write_text("patch content", encoding="utf-8")

    repo.create_task(
        task_id="T-BAK1",
        idem_key="k-bak1",
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="x",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(t1_dir),
    )
    repo.finalize_task("T-BAK1", "FINISHED", "resolved")
    return repo, runs_dir


def test_create_and_verify_backup(tmp_path: Path) -> None:
    repo, runs_dir = _setup_test_env(tmp_path)
    backups_root = tmp_path / "backups"

    manifest = create_backup(repo, backups_root, runs_root=runs_dir, backup_id="bak-test-1")
    assert manifest["backup_id"] == "bak-test-1"
    assert manifest["tasks_count"] == 1

    bak_dir = backups_root / "bak-test-1"
    assert (bak_dir / "backup_manifest.json").is_file()
    assert (bak_dir / "patchpilot.sqlite3").is_file()
    assert (bak_dir / "runs" / "T-BAK1" / "report.json").is_file()
    assert (bak_dir / "runs" / "T-BAK1" / "reports" / "test.xml").is_file()
    # 验证可弃集 workspace 不进入备份包
    assert not (bak_dir / "runs" / "T-BAK1" / "workspace").exists()

    # 校验合法备份包
    is_valid, errors = verify_backup(bak_dir)
    assert is_valid is True
    assert not errors


def test_verify_corrupted_backup_fails(tmp_path: Path) -> None:
    repo, runs_dir = _setup_test_env(tmp_path)
    backups_root = tmp_path / "backups"
    create_backup(repo, backups_root, runs_root=runs_dir, backup_id="bak-tamper")

    bak_dir = backups_root / "bak-tamper"
    # 篡改报告文件
    rep_file = bak_dir / "runs" / "T-BAK1" / "report.json"
    rep_file.write_text('{"tampered": true}', encoding="utf-8")

    is_valid, errors = verify_backup(bak_dir)
    assert is_valid is False
    assert any("sha256 mismatch" in e for e in errors)


def test_restore_backup_to_empty_dir(tmp_path: Path) -> None:
    repo, runs_dir = _setup_test_env(tmp_path)
    backups_root = tmp_path / "backups"
    create_backup(repo, backups_root, runs_root=runs_dir, backup_id="bak-restore")
    bak_dir = backups_root / "bak-restore"

    restore_target = tmp_path / "restored_inst"
    res = restore_backup(bak_dir, restore_target)
    assert res["status"] == "success"
    assert res["tasks_restored"] == 1

    # 验证恢复后数据库与取证工件
    restored_repo = Repository(restore_target / "patchpilot.sqlite3")
    task = restored_repo.get_task("T-BAK1")
    assert task is not None
    assert task["verdict"] == "resolved"
    assert (restore_target / "runs" / "T-BAK1" / "report.json").is_file()


def test_restore_refuses_non_empty_dir_without_overwrite(tmp_path: Path) -> None:
    repo, runs_dir = _setup_test_env(tmp_path)
    backups_root = tmp_path / "backups"
    create_backup(repo, backups_root, runs_root=runs_dir, backup_id="bak-safe")
    bak_dir = backups_root / "bak-safe"

    restore_target = tmp_path / "dirty_target"
    restore_target.mkdir()
    (restore_target / "existing.txt").write_text("pre-existing file", encoding="utf-8")

    # 未传 overwrite=True 时拒绝覆盖
    with pytest.raises(ValueError, match="is not empty"):
        restore_backup(bak_dir, restore_target, overwrite=False)

    # 传 overwrite=True 时允许覆盖
    res = restore_backup(bak_dir, restore_target, overwrite=True)
    assert res["status"] == "success"


def test_backup_restore_cli_flow(tmp_path: Path) -> None:
    _repo, runs_dir = _setup_test_env(tmp_path)
    backups_root = tmp_path / "cli_backups"

    # 1. CLI 备份
    code_bak = cli_main(
        [
            "backup",
            "--target",
            str(backups_root),
            "--db",
            str(tmp_path / "origin.sqlite3"),
            "--runs",
            str(runs_dir),
        ]
    )
    assert code_bak == 0

    bak_dirs = list(backups_root.glob("bak-*"))
    assert len(bak_dirs) == 1
    bak_dir = bak_dirs[0]

    # 2. CLI 校验
    code_ver = cli_main(["verify", "--backup", str(bak_dir)])
    assert code_ver == 0

    # 3. CLI 恢复
    restore_target = tmp_path / "cli_restored"
    code_res = cli_main(["restore", "--backup", str(bak_dir), "--target", str(restore_target)])
    assert code_res == 0
    assert (restore_target / "patchpilot.sqlite3").is_file()
