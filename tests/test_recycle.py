"""P3-12 终态产物回收测试:取证集永久、可弃集回收、Grace 扫描、Windows 重试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api.recycle import recycle_finished_tasks, recycle_run_dir
from app.config import get_settings
from app.storage.repository import Repository


def _make_run_dir(root: Path, task_id: str) -> Path:
    """典型 graph 任务 run 目录:取证文件 + 可弃集(workspace 96K/checkpoints 68K)。"""
    run_dir = root / "runs" / task_id
    (run_dir / "workspace" / "src").mkdir(parents=True)
    (run_dir / "workspace" / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (run_dir / "reports").mkdir()
    (run_dir / "reports" / "verify-failed.xml").write_text("<x/>", encoding="utf-8")
    (run_dir / "checkpoints.sqlite").write_bytes(b"sqlite")
    (run_dir / "report.json").write_text("{}", encoding="utf-8")
    (run_dir / "diff.patch").write_text("diff", encoding="utf-8")
    (run_dir / "trajectory.jsonl").write_text("{}", encoding="utf-8")
    return run_dir


def test_recycle_run_dir_removes_disposable_keeps_forensics(tmp_path: Path) -> None:
    """可弃集(workspace/checkpoints)删净;取证文件(report/diff/轨迹/junit)保留。"""
    run_dir = _make_run_dir(tmp_path, "T-1")

    removed = recycle_run_dir(run_dir)

    assert removed == 2
    assert not (run_dir / "workspace").exists()
    assert not (run_dir / "checkpoints.sqlite").exists()
    assert (run_dir / "report.json").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "trajectory.jsonl").exists()
    assert (run_dir / "reports" / "verify-failed.xml").exists()


def test_recycle_survives_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 句柄被短暂持有(sharing violation → PermissionError)时
    重试后成功;持续失败不抛出(不阻断任务收尾)。"""
    import app.api.recycle as recycle_mod

    run_dir = _make_run_dir(tmp_path, "T-1")
    real_rmtree = recycle_mod.shutil.rmtree
    calls = {"n": 0}

    def flaky_rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(f"[WinError 32] sharing violation: {path}")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(recycle_mod.shutil, "rmtree", flaky_rmtree)
    assert recycle_run_dir(run_dir) == 2
    assert calls["n"] == 2  # 第一次抛 sharing violation,重试成功

    run_dir2 = _make_run_dir(tmp_path, "T-2")

    def always_blocked(*args, **kwargs):
        raise PermissionError("[WinError 32] sharing violation")

    monkeypatch.setattr(recycle_mod.shutil, "rmtree", always_blocked)
    monkeypatch.setattr(Path, "unlink", always_blocked)
    assert recycle_run_dir(run_dir2) == 0  # 放弃本次,取证文件仍完整
    assert (run_dir2 / "report.json").exists()
    assert (run_dir2 / "workspace").exists()  # 回收失败不半删目录


def test_grace_scan_recycles_only_old_finished_tasks(tmp_path: Path) -> None:
    """启动 Grace 扫描:finished_at 早于 grace 截止的 FINISHED 才回收;
    刚结束的留人看;非 FINISHED 状态不回收。"""
    repo = Repository(tmp_path / "db.sqlite3")
    old_finished = _make_run_dir(tmp_path, "T-OLD")
    fresh_finished = _make_run_dir(tmp_path, "T-FRESH")
    cancelled = _make_run_dir(tmp_path, "T-CANCELLED")
    now = datetime.now(UTC)
    old_stamp = (now - timedelta(hours=3)).isoformat(timespec="milliseconds")
    fresh_stamp = now.isoformat(timespec="milliseconds")
    for tid, idem, run_dir in (
        ("T-OLD", "a", old_finished),
        ("T-FRESH", "b", fresh_finished),
        ("T-CANCELLED", "c", cancelled),
    ):
        repo.create_task(
            task_id=tid,
            idem_key=idem,
            bug_id="BUG-001",
            repo_path="bugs/BUG-001/repo",
            issue_text="x",
            max_rounds=5,
            engine="graph",
            model_provider="fake",
            run_dir=str(run_dir),
        )
    repo.finalize_task("T-OLD", "FINISHED", "resolved")
    repo.finalize_task("T-FRESH", "FINISHED", "resolved")
    repo.finalize_task("T-CANCELLED", "CANCELLED", "cancelled")
    # 手工把 T-OLD 的 finished_at 拨回 3 小时前(finalize 用的当前时钟)
    with repo._lock, repo._conn:
        repo._conn.execute("UPDATE tasks SET finished_at=? WHERE id=?", (old_stamp, "T-OLD"))
        repo._conn.commit()
    _ = fresh_stamp

    recycled = recycle_finished_tasks(repo, grace_seconds=3600)

    assert recycled == ["T-OLD"]
    assert not (old_finished / "workspace").exists()
    assert (old_finished / "report.json").exists()  # 取证集仍在
    assert (fresh_finished / "workspace").exists()  # grace 内不回收
    assert (cancelled / "workspace").exists()  # 取消现场不回收


def test_service_recycles_workspace_on_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端:service 跑完 FINISHED 任务 → 终态回写后回收可弃集,取证文件与
    DB 终态完整。

    P3-12 语义钉死(本测试同时证明两件事):
    ① 回收是**最终一致而非瞬时**——注入 1s 慢回收后,观察到 FINISHED 时
       workspace 仍在(终态回写先于回收完成,观察窗口真实存在;全量 pytest
       曾因此出现 1 次 flake,已按此根因修复测试的瞬时假设);
    ② 有界等待内**必须被回收**——断言强度不变,只去掉原子性假设。
    """
    import time

    import app.api.service as service_mod
    from app.api.service import TaskService
    from app.storage.repository import TERMINAL_STATUSES

    settings = get_settings()
    assert settings.recycle_finished_workspace is True  # 默认开(P3-12)

    # 复用 test_service_robustness 的 tiny fixture(空白串缺陷,已验证可 FINISHED)
    from tests.test_service_robustness import (
        _TINY_FIX_DIFF,
        _materialize_tiny_repo,
    )

    real_recycle = service_mod.recycle_run_dir
    observed: dict[str, bool] = {}

    def slow_recycle(run_dir: Path) -> int:
        # 先取证再拖时:记录"进入回收时 workspace 仍在"(此刻 DB 已是 FINISHED)
        observed["workspace_during_window"] = (Path(run_dir) / "workspace").exists()
        time.sleep(1.0)
        return real_recycle(run_dir)

    monkeypatch.setattr(service_mod, "recycle_run_dir", slow_recycle)

    repo_root = tmp_path / "tiny-repo"
    _materialize_tiny_repo(repo_root)
    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
    )
    replay = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "apply_patch", "args": {"diff_text": _TINY_FIX_DIFF}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "fixed empty input"}},
    ]
    task, created = service.create_task(
        repo_path=str(repo_root),
        issue_text="empty string crash",
        failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
        regression_tests=[
            "tests/test_dateparse.py::test_iso_format",
            "tests/test_dateparse.py::test_none_returns_none",
        ],
        allowed_paths=["src/dateparse.py"],
        replay_script=replay,
        engine="plain",
    )
    assert created
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        row = service.repo.get_task(task["task_id"])
        if row["status"] in TERMINAL_STATUSES:
            break
        time.sleep(0.2)
    service.shutdown()

    final = service.repo.get_task(task["task_id"])
    assert final["status"] == "FINISHED", final
    run_dir = Path(final["run_dir"])
    # ① 窗口确实存在(有界等待取证,去计时巧合):DB 已 FINISHED 时,进入回收的
    #    那一刻 workspace 仍在——终态回写先于回收完成,观察者不得假设原子性
    window_deadline = time.monotonic() + 10
    while "workspace_during_window" not in observed and time.monotonic() < window_deadline:
        time.sleep(0.05)
    assert observed.get("workspace_during_window") is True, (
        "预期回收窗口内 workspace 仍在(证明终态回写先于回收完成)"
    )
    # ② 有界等待内必须回收:断言强度不变,只不假设原子性
    deadline = time.monotonic() + 30
    while (run_dir / "workspace").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not (run_dir / "workspace").exists(), "FINISHED 任务的可弃集必须已回收"
    assert not (run_dir / "checkpoints.sqlite").exists()
    assert (run_dir / "report.json").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "trajectory.jsonl").exists()
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "resolved"


def test_recycle_handles_readonly_git_objects(tmp_path: Path) -> None:
    """工作区是物化 git 仓库,git objects 落盘为只读文件(444):Windows 的
    rmtree 对它们直接 WinError 5——回收器必须整树去只读后删除(实测踩中)。"""
    import stat

    run_dir = _make_run_dir(tmp_path, "T-RO")
    obj_dir = run_dir / "workspace" / ".git" / "objects" / "ab"
    obj_dir.mkdir(parents=True)
    obj_file = obj_dir / "deadbeef"
    obj_file.write_bytes(b"x")
    obj_file.chmod(stat.S_IREAD)  # git objects 落盘形态:只读

    assert recycle_run_dir(run_dir) == 2
    assert not (run_dir / "workspace").exists()
    assert (run_dir / "report.json").exists()
