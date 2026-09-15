"""M7 API 端到端测试:创建 → 轮询 → 轨迹 → 报告 全流程 + 幂等/取消/错误结构。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "api.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


def wait_terminal(client: TestClient, task_id: str, timeout_s: int = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.5)
    pytest.fail(f"task {task_id} did not finish in {timeout_s}s")


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_full_task_lifecycle(client: TestClient) -> None:
    """从 API 创建任务到查询最终报告的完整流程(plain 引擎,回放模型)。"""
    resp = client.post("/api/tasks", json={"bug_id": "BUG-003", "engine": "plain", "model": "fake"})
    assert resp.status_code == 201, resp.text
    task = resp.json()
    task_id = task["task_id"]
    assert task["status"] in {"QUEUED", "RUNNING"}

    final = wait_terminal(client, task_id)
    assert final["status"] == "FINISHED" and final["verdict"] == "resolved"

    # 轨迹分页
    traj = client.get(f"/api/tasks/{task_id}/trajectory?limit=100").json()
    assert traj["total_returned"] > 0
    tools = {e["tool"] for e in traj["events"]}
    assert "apply_patch" in tools and "run_tests" in tools

    # 报告(JSON + Markdown)
    report_json = client.get(f"/api/tasks/{task_id}/report")
    assert report_json.status_code == 200
    assert report_json.json()["verdict"] == "resolved"
    report_md = client.get(f"/api/tasks/{task_id}/report?format=markdown")
    assert report_md.status_code == 200
    assert "判定过程" in report_md.text and "resolved" in report_md.text

    # 任务列表
    tasks = client.get("/api/tasks").json()["tasks"]
    assert any(t["task_id"] == task_id for t in tasks)


def test_create_is_idempotent(client: TestClient) -> None:
    first = client.post("/api/tasks", json={"bug_id": "BUG-004", "engine": "plain"}).json()
    second = client.post("/api/tasks", json={"bug_id": "BUG-004", "engine": "plain"}).json()
    assert first["task_id"] == second["task_id"]  # 在途任务幂等


def test_error_structure(client: TestClient) -> None:
    missing = client.post("/api/tasks", json={"bug_id": "BUG-9999"})
    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == {"code", "message", "task_id"} and body["code"] == "invalid_task"

    assert client.get("/api/tasks/missing").status_code == 404
    assert client.get("/api/tasks/missing/trajectory").status_code == 404
    assert client.get("/api/tasks/missing/report").status_code == 404
    assert client.post("/api/tasks/missing/cancel").status_code == 404


def test_report_not_ready_returns_409(client: TestClient) -> None:
    task = client.post("/api/tasks", json={"bug_id": "BUG-005", "engine": "plain"}).json()
    # 任务刚开始执行,报告几乎必然尚未生成(9s 级任务)
    resp = client.get(f"/api/tasks/{task['task_id']}/report")
    assert resp.status_code in {200, 409}
    if resp.status_code == 409:
        assert resp.json()["code"] == "not_ready"
    wait_terminal(client, task["task_id"])


def test_cancel_finished_conflicts(client: TestClient) -> None:
    task = client.post("/api/tasks", json={"bug_id": "BUG-002", "engine": "plain"}).json()
    wait_terminal(client, task["task_id"])
    resp = client.post(f"/api/tasks/{task['task_id']}/cancel")
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


def test_boot_recovery_marks_stale_running(tmp_path: Path) -> None:
    """重启恢复:上一进程的 RUNNING 任务在新服务启动时转 NEEDS_REVIEW。"""
    from app.storage.repository import Repository

    db = tmp_path / "recover.sqlite3"
    repo = Repository(db)
    repo.create_task(
        task_id="T-STALE",
        idem_key="x",
        bug_id="BUG-001",
        repo_path=".",
        issue_text="",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "run"),
    )
    repo.update_task_status("T-STALE", "RUNNING")

    app = create_app(db_path=db, runs_root=tmp_path / "runs")
    with TestClient(app):
        assert repo.get_task("T-STALE")["status"] == "NEEDS_REVIEW"
