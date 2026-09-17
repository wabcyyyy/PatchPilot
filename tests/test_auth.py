"""N1:可选 Bearer Token 鉴权(api_token 为空 = 关闭,health 恒豁免)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import get_settings

AUTH = {"Authorization": "Bearer secret-token"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "auth.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def secured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PATCHPILOT_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app = create_app(db_path=tmp_path / "auth-secured.sqlite3", runs_root=tmp_path / "runs-sec")
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()  # 结束后恢复缓存(env 由 monkeypatch 复原)


def test_token_unconfigured_allows_anonymous(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    # 未配置时匿名请求直达路由层:BUG-9999 返回 404 而非 401
    missing = client.post("/api/tasks", json={"bug_id": "BUG-9999"})
    assert missing.status_code == 404 and missing.json()["code"] == "invalid_task"


def test_secured_missing_header_401(secured: TestClient) -> None:
    resp = secured.get("/api/tasks")
    assert resp.status_code == 401
    body = resp.json()
    assert set(body) == {"code", "message", "task_id"}
    assert body["code"] == "unauthorized" and body["task_id"] is None


def test_secured_wrong_token_401(secured: TestClient) -> None:
    resp = secured.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401 and resp.json()["code"] == "unauthorized"
    raw = secured.get("/api/tasks", headers={"Authorization": "secret-token"})  # 缺 Bearer 前缀
    assert raw.status_code == 401


def test_secured_valid_token_passes(secured: TestClient) -> None:
    created = secured.post(
        "/api/tasks", json={"bug_id": "BUG-002", "engine": "plain", "model": "fake"}, headers=AUTH
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert secured.get("/api/tasks", headers=AUTH).status_code == 200
    assert secured.get(f"/api/tasks/{task_id}", headers=AUTH).status_code == 200


def test_health_exempt_when_secured(secured: TestClient) -> None:
    assert secured.get("/api/health").json() == {"status": "ok"}


def test_report_trajectory_cancel_protected(secured: TestClient) -> None:
    for method, url in [
        ("GET", "/api/tasks/missing/report"),
        ("GET", "/api/tasks/missing/trajectory"),
        ("POST", "/api/tasks/missing/cancel"),
    ]:
        resp = getattr(secured, method.lower())(url)
        assert resp.status_code == 401, f"{method} {url}"
        assert resp.json()["code"] == "unauthorized"
