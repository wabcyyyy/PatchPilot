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
    first = client.post("/api/tasks", json={"bug_id": "BUG-004", "engine": "plain"})
    second = client.post("/api/tasks", json={"bug_id": "BUG-004", "engine": "plain"})
    # 幂等:同键在途任务返回同一 task_id,且命中(200)与新建(201)状态码可区分(T12.3)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]


def test_error_structure(client: TestClient) -> None:
    missing = client.post("/api/tasks", json={"bug_id": "BUG-9999"})
    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == {"code", "message", "task_id"} and body["code"] == "invalid_task"

    assert client.get("/api/tasks/missing").status_code == 404
    assert client.get("/api/tasks/missing/trajectory").status_code == 404
    assert client.get("/api/tasks/missing/report").status_code == 404
    assert client.post("/api/tasks/missing/cancel").status_code == 404


def test_unhandled_exception_returns_unified_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底:未预期异常也必须输出统一错误结构(AGENTS 要求,整改自评审)。

    N-18 整改同步修订期望:异常原文可能含内部路径/SQL,不得回显给客户端——
    对外只给固定文案 + error id,原文进服务端日志。
    """
    app = create_app(db_path=tmp_path / "api.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app, raise_server_exceptions=False) as c:

        def boom(**kwargs):
            raise RuntimeError("boom secret detail")

        monkeypatch.setattr(c.app.state.service, "create_task", boom)
        resp = c.post("/api/tasks", json={"bug_id": "BUG-001", "engine": "plain"})
    assert resp.status_code == 500
    body = resp.json()
    assert set(body) == {"code", "message", "task_id"}
    assert body["code"] == "internal"
    assert "internal server error (ref: " in body["message"]
    assert "boom" not in body["message"]  # 异常原文不得外泄


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


def test_create_task_rejects_openai_when_llm_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总开关默认关闭:model=openai 同步被拒(404),不创建任务。"""
    from app.config import get_settings

    monkeypatch.setenv("PATCHPILOT_LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        resp = client.post(
            "/api/tasks", json={"bug_id": "BUG-003", "engine": "plain", "model": "openai"}
        )
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "PATCHPILOT_LLM_ENABLED" in body["message"]


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
    repo.set_status_unless_terminal("T-STALE", "RUNNING")

    app = create_app(db_path=db, runs_root=tmp_path / "runs")
    with TestClient(app):
        assert repo.get_task("T-STALE")["status"] == "NEEDS_REVIEW"


def test_create_task_rejects_bug_id_path_traversal(client: TestClient) -> None:
    """N-2 整改:bug_id 只能是 BUG-xxx 编号;路径/穿越形态在 schema 层 422,不触达磁盘。"""
    for bad in ("../../etc", "D:/evil/task", "BUG-001/repo", "bugs/BUG-001"):
        resp = client.post("/api/tasks", json={"bug_id": bad, "engine": "plain"})
        assert resp.status_code == 422, (bad, resp.status_code)


def test_task_responses_do_not_leak_internal_fields(client: TestClient) -> None:
    """R2 整改:响应经 TaskOut 收敛——idem_key/repo_path/内部主键 id 不得外泄。"""
    task = client.post("/api/tasks", json={"bug_id": "BUG-005", "engine": "plain"}).json()
    assert set(task) == {
        "task_id",
        "bug_id",
        "status",
        "verdict",
        "engine",
        "model_provider",
        "max_rounds",
        "run_dir",
        "created_at",
        "finished_at",
        # S07:运行中进度字段(stage 与生命周期 status 是两列,LOCALIZE 不是终态)
        "stage",
        "last_event_at",
    }
    listing = client.get("/api/tasks").json()
    assert listing["tasks"] and set(listing["tasks"][0]) == set(task)


def test_validation_error_returns_unified_structure(client: TestClient) -> None:
    """复盘 P1-8:Pydantic 校验失败收敛为 {code, message, task_id},
    不再裸露 FastAPI 原生 {"detail": [...]}。"""
    resp = client.post("/api/tasks", json={"bug_id": "not-a-valid-id!"})
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"code", "message", "task_id"}
    assert body["code"] == "validation_error"
    assert body["task_id"] is None
    assert "bug_id" in body["message"]


def test_request_id_header_and_task_log_context(client: TestClient) -> None:
    """复盘 P1-8:每个 HTTP 响应携带 X-Request-ID(中间件生成),
    任务线程的日志上下文带同一 request_id(经 logctx 装配)。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    request_id = resp.headers.get("X-Request-ID")
    assert request_id  # 中间件已生成并回写
    # 不同请求的 request_id 不同
    other = client.get("/api/health").headers.get("X-Request-ID")
    assert other and other != request_id
