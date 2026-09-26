"""M7 存储层测试:CRUD、幂等键、僵尸恢复、轨迹索引。"""

from __future__ import annotations

from pathlib import Path

from app.storage.repository import Repository


def _make_task(repo: Repository, task_id: str, idem: str) -> None:
    repo.create_task(
        task_id=task_id,
        idem_key=idem,
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="empty string",
        max_rounds=5,
        engine="graph",
        model_provider="fake-replay",
        run_dir=f"runs/{task_id}",
    )


def test_task_lifecycle(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    _make_task(repo, "T1", "idem-1")
    task = repo.get_task("T1")
    assert task and task["status"] == "QUEUED"

    repo.set_status_unless_terminal("T1", "RUNNING")
    assert repo.get_task("T1")["status"] == "RUNNING"

    repo.finalize_task("T1", "FINISHED", "resolved")
    finished = repo.get_task("T1")
    assert finished["status"] == "FINISHED" and finished["verdict"] == "resolved"
    assert finished["finished_at"]

    assert repo.get_task("missing") is None
    assert len(repo.list_tasks()) == 1


def test_idempotency_key_lookup(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    _make_task(repo, "T1", "idem-A")
    _make_task(repo, "T2", "idem-B")
    assert repo.find_by_idem_key("idem-A")["id"] == "T1"
    assert repo.find_by_idem_key("nope") is None


def test_recover_stale_running(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    _make_task(repo, "T1", "a")
    _make_task(repo, "T2", "b")
    _make_task(repo, "T3", "c")
    repo.set_status_unless_terminal("T1", "RUNNING")
    repo.set_status_unless_terminal("T2", "RUNNING")
    repo.finalize_task("T3", "FINISHED", "resolved")

    # N-20 整改:返回被恢复行的 idem_key,供 service 同步清残留锁
    stale_keys = repo.recover_stale_running()
    assert sorted(stale_keys) == ["a", "b"]
    assert repo.get_task("T1")["status"] == "NEEDS_REVIEW"
    assert repo.get_task("T3")["status"] == "FINISHED"
    assert repo.recover_stale_running() == []  # 幂等


def test_trajectory_roundtrip(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    events = [
        {
            "event_id": f"e{i}",
            "task_id": "T1",
            "round": 1,
            "state": "LOCALIZE",
            "tool": "search_code",
            "request_id": f"r{i}",
            "input": {"keyword": "k"},
            "output_summary": {"matches": i},
            "duration_ms": 5,
            "error": None,
            "timestamp": "2026-09-16T00:00:00Z",
        }
        for i in range(5)
    ]
    assert repo.insert_events("T1", events) == 5
    page = repo.get_trajectory("T1", limit=3, offset=0)
    assert len(page) == 3 and page[0]["input"] == {"keyword": "k"}
    page2 = repo.get_trajectory("T1", limit=3, offset=3)
    assert len(page2) == 2


def test_trajectory_query_uses_index(tmp_path: Path) -> None:
    from app.storage.db import connect

    conn = connect(tmp_path / "t.sqlite3")
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM trajectory_events WHERE task_id = ? ORDER BY id",
        ("T1",),
    ).fetchall()
    detail = " ".join(dict(row)["detail"] for row in plan)
    assert "idx_traj_task" in detail


def test_patch_insert(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    repo.insert_patch(
        task_id="T1",
        round_no=1,
        diff_path="runs/T1/diff.patch",
        changed_files=["src/a.py"],
        gate_result="passed",
        applied=True,
    )
    assert True  # 写入不抛异常即通过;读取由 API e2e 覆盖
    # insert_test_run 用例已随 P1-5 整改删除(test_runs 表无生产调用方)
    # test_evaluation_upsert 用例已随 P3-8 整改删除(evaluations 表零生产读方)


def test_finalize_and_cancel_are_atomically_guarded(tmp_path: Path) -> None:
    """N-7 整改:终态回写与取消互相竞争时,单向让位、不可互相覆盖。

    此前两侧都是"先读后写 + 无条件 UPDATE",已 resolved 任务的终态可被并发
    cancel 改写;现在守卫下沉为单条语句并以 rowcount 判定输赢。
    """
    repo = Repository(tmp_path / "t.sqlite3")
    _make_task(repo, "T1", "idem-1")
    _make_task(repo, "T2", "idem-2")
    repo.set_status_unless_terminal("T1", "RUNNING")
    repo.set_status_unless_terminal("T2", "RUNNING")

    # 方向 a:取消后自然完成不得覆盖 CANCELLED
    assert repo.cancel_task_row("T1") is True
    assert repo.finalize_task("T1", "FINISHED", "resolved") is False
    after = repo.get_task("T1")
    assert after["status"] == "CANCELLED" and after["verdict"] == "cancelled"

    # 方向 b:终态后取消不得生效
    assert repo.finalize_task("T2", "FINISHED", "resolved") is True
    assert repo.cancel_task_row("T2") is False
    finished = repo.get_task("T2")
    assert finished["status"] == "FINISHED" and finished["verdict"] == "resolved"

    # 方向 c(P3-4):终态之间的改写一律无效,谁先到终态谁赢
    assert repo.finalize_task("T2", "BUDGET_EXCEEDED", "failed") is False
    after = repo.get_task("T2")
    assert after["status"] == "FINISHED" and after["verdict"] == "resolved"

    # 非终态流转(RUNNING)仍走无条件写入
    _make_task(repo, "T3", "idem-3")
    repo.set_status_unless_terminal("T3", "RUNNING")
    assert repo.get_task("T3")["status"] == "RUNNING"


def test_finalize_cannot_revive_needs_review(tmp_path: Path) -> None:
    """P3-4:NEEDS_REVIEW(recover_stale 的收敛值)不得被 finalize(FINISHED)
    复活——多进程共库时 B 进程把 A 的在途任务收敛 NEEDS_REVIEW,A 跑完
    finalize 不得改写;行保持 NEEDS_REVIEW 等人复核。"""
    repo = Repository(tmp_path / "t.sqlite3")
    _make_task(repo, "T1", "idem-1")
    repo.set_status_unless_terminal("T1", "RUNNING")
    # 模拟 recover_stale / 另一进程的收敛
    assert repo.finalize_task("T1", "NEEDS_REVIEW", "needs_review") is True
    # 原进程跑完后的 finalize 必须输掉竞争
    assert repo.finalize_task("T1", "FINISHED", "resolved") is False
    row = repo.get_task("T1")
    assert row["status"] == "NEEDS_REVIEW" and row["verdict"] == "needs_review"
