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

    repo.update_task_status("T1", "RUNNING")
    assert repo.get_task("T1")["status"] == "RUNNING"

    repo.update_task_status("T1", "FINISHED", "resolved")
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
    repo.update_task_status("T1", "RUNNING")
    repo.update_task_status("T2", "RUNNING")
    repo.update_task_status("T3", "FINISHED", "resolved")

    count = repo.recover_stale_running()
    assert count == 2
    assert repo.get_task("T1")["status"] == "NEEDS_REVIEW"
    assert repo.get_task("T3")["status"] == "FINISHED"
    assert repo.recover_stale_running() == 0  # 幂等


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


def test_evaluation_upsert(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    repo.upsert_evaluation(task_id="T1", bug_id="BUG-001", final_resolved=1, rounds=2)
    repo.upsert_evaluation(task_id="T1", bug_id="BUG-001", final_resolved=1, rounds=3, tokens=100)
    rows = repo.list_evaluations()
    assert len(rows) == 1 and rows[0]["rounds"] == 3 and rows[0]["tokens"] == 100


def test_patch_and_test_run_insert(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.sqlite3")
    repo.insert_patch(
        task_id="T1",
        round_no=1,
        diff_path="runs/T1/diff.patch",
        changed_files=["src/a.py"],
        gate_result="passed",
        applied=True,
    )
    repo.insert_test_run(
        task_id="T1",
        round_no=1,
        kind="baseline",
        passed=2,
        failed=1,
        errors=0,
        exit_code=1,
        report_path="reports/x.xml",
        duration_ms=900,
    )
    assert True  # 写入不抛异常即通过;读取由 API e2e 覆盖
