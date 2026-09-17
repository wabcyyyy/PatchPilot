"""N4a/N4b:任意仓库任务(repo_path+issue)—— 校验矩阵、e2e 与门禁攻击面。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.gitops.testing import materialize_repo

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "custom.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def custom_repo(tmp_path: Path, demo_repo: Path) -> Path:
    """物化一份演示仓库作为用户的"自有仓库"。"""
    dest = tmp_path / "my-repo"
    materialize_repo(demo_repo, dest)
    return dest


def _custom_payload(repo: Path, **overrides: object) -> dict:
    payload: dict = {
        "repo_path": str(repo),
        "issue_text": "空字符串输入时 parse_date 抛异常,应返回 None",
        "failed_tests": [FAILED_ID],
        "regression_tests": REGRESSION_IDS,
        "engine": "plain",
        "model": "fake",
        "replay_script": [{"tool": "finish", "args": {"success": True, "summary": "s"}}],
    }
    payload.update(overrides)
    return payload


# ---------- N4a:校验矩阵 ----------


def test_both_bug_id_and_repo_path_rejected(client: TestClient, custom_repo: Path) -> None:
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo, bug_id="BUG-001"))
    assert resp.status_code == 422


def test_neither_bug_id_nor_repo_path_rejected(client: TestClient) -> None:
    resp = client.post("/api/tasks", json={"engine": "plain"})
    assert resp.status_code == 422


def test_repo_path_without_required_fields_rejected(client: TestClient, custom_repo: Path) -> None:
    for missing in ("issue_text", "failed_tests", "regression_tests"):
        payload = _custom_payload(custom_repo)
        payload.pop(missing)
        resp = client.post("/api/tasks", json=payload)
        assert resp.status_code == 422, missing
        assert missing in resp.text


def test_repo_path_not_found_returns_404(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/tasks",
        json=_custom_payload(tmp_path / "no-such-dir"),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "repo_path" in body["message"]


def test_custom_fake_without_script_rejected(client: TestClient, custom_repo: Path) -> None:
    """自定义任务 + fake + 无脚本 → 404,提示二选一(补脚本或换 openai)。"""
    payload = _custom_payload(custom_repo)
    payload.pop("replay_script")
    resp = client.post("/api/tasks", json=payload)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task"
    assert "replay_script" in body["message"] and "openai" in body["message"]


def test_custom_openai_disabled_rejected(client: TestClient, custom_repo: Path) -> None:
    """自定义任务 + openai 走现有总开关前置校验(默认关闭 → 404,不建任务)。"""
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo, model="openai"))
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "PATCHPILOT_LLM_ENABLED" in body["message"]


def test_valid_custom_task_created(client: TestClient, custom_repo: Path) -> None:
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo))
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["bug_id"].startswith("CUSTOM-")
    assert task["status"] in {"QUEUED", "RUNNING"}


def test_custom_tasks_never_idempotent(client: TestClient, custom_repo: Path) -> None:
    """CUSTOM id 含随机段:同一请求两次创建是两个独立任务(正式题仍幂等,见 test_api)。"""
    first = client.post("/api/tasks", json=_custom_payload(custom_repo)).json()
    second = client.post("/api/tasks", json=_custom_payload(custom_repo)).json()
    assert first["task_id"] != second["task_id"]
