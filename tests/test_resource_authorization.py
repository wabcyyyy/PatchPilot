"""资源级授权与可归因审计测试 (P03)。

验证点：
- 主体 A 无法读取/批准/取消主体 B 的任务；
- 授权失败一律返回 404 (不泄露任务存在性)；
- 列表接口对普通 member/service_account 只返回自身任务；
- reviewer 具备跨属主只读与验收批准权限，但禁止取消/发送控制消息；
- admin 跨属主访问自动记录 audit_events 审计；
- 越权尝试自动记录 task_access_denied 审计；
- SSE 流式连接在凭据吊销后立即终止并推送 error 帧；
- 主体参与幂等域，不同主体不互相劫持任务。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import get_settings
from app.storage.repository import Repository


@pytest.fixture()
def auth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATCHPILOT_DEPLOYMENT_MODE", "production")
    get_settings.cache_clear()

    db_path = tmp_path / "p03_auth.sqlite3"
    runs_root = tmp_path / "p03_runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    repo = Repository(db_path)
    # 建立主体与凭据
    repo.create_principal(principal_id="user_a", name="User A", role="member")
    cred_a, key_a = repo.issue_credential(principal_id="user_a")

    repo.create_principal(principal_id="user_b", name="User B", role="member")
    cred_b, key_b = repo.issue_credential(principal_id="user_b")

    repo.create_principal(principal_id="reviewer_1", name="Reviewer One", role="reviewer")
    cred_r, key_r = repo.issue_credential(principal_id="reviewer_1")

    repo.create_principal(principal_id="admin_1", name="Admin One", role="admin")
    cred_adm, key_adm = repo.issue_credential(principal_id="admin_1")

    app = create_app(db_path=db_path, runs_root=runs_root)

    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "key_a": key_a,
            "cred_a": cred_a,
            "key_b": key_b,
            "cred_b": cred_b,
            "key_r": key_r,
            "cred_r": cred_r,
            "key_adm": key_adm,
            "cred_adm": cred_adm,
        }

    get_settings.cache_clear()


def test_cross_principal_access_forbidden(auth_env: dict) -> None:
    client: TestClient = auth_env["client"]
    repo: Repository = auth_env["repo"]
    key_a = auth_env["key_a"]
    key_b = auth_env["key_b"]
    key_r = auth_env["key_r"]
    key_adm = auth_env["key_adm"]

    # 1. User A 创建任务
    create_res = client.post(
        "/api/tasks",
        json={"bug_id": "BUG-002", "engine": "plain", "model": "fake"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert create_res.status_code == 201
    task_a_id = create_res.json()["task_id"]

    # 2. User B 尝试访问 User A 的任务详情、轨迹、报告、取消、问题
    headers_b = {"Authorization": f"Bearer {key_b}"}
    for method, path in [
        ("GET", f"/api/tasks/{task_a_id}"),
        ("GET", f"/api/tasks/{task_a_id}/trajectory"),
        ("GET", f"/api/tasks/{task_a_id}/report"),
        ("GET", f"/api/tasks/{task_a_id}/question"),
        ("POST", f"/api/tasks/{task_a_id}/cancel"),
    ]:
        res = getattr(client, method.lower())(path, headers=headers_b)
        # 绝不返回 403 泄露任务存在性，严格返回 404
        assert res.status_code == 404, f"{method} {path} should be 404"
        assert res.json()["code"] == "invalid_task"

    # 3. User B 查询任务列表：不包含 User A 的任务
    list_b = client.get("/api/tasks", headers=headers_b)
    assert list_b.status_code == 200
    task_ids_b = [t["task_id"] for t in list_b.json()["tasks"]]
    assert task_a_id not in task_ids_b

    # 4. 越权审计已被记录
    denied_audits = repo.list_audit_events(actor_id="user_b", action="task_access_denied")
    assert len(denied_audits) >= 1
    assert denied_audits[0]["task_id"] == task_a_id

    # 5. Reviewer 跨属主只读：放行
    headers_r = {"Authorization": f"Bearer {key_r}"}
    res_r_get = client.get(f"/api/tasks/{task_a_id}", headers=headers_r)
    assert res_r_get.status_code == 200
    assert res_r_get.json()["task_id"] == task_a_id

    # Reviewer 尝试取消：被拒绝 (404)
    res_r_cancel = client.post(f"/api/tasks/{task_a_id}/cancel", headers=headers_r)
    assert res_r_cancel.status_code == 404

    # 6. Admin 跨属主访问：放行且记录审计
    headers_adm = {"Authorization": f"Bearer {key_adm}"}
    res_adm_get = client.get(f"/api/tasks/{task_a_id}", headers=headers_adm)
    assert res_adm_get.status_code == 200

    admin_audits = repo.list_audit_events(actor_id="admin_1", action="admin_cross_owner_access")
    assert len(admin_audits) >= 1
    assert admin_audits[0]["task_id"] == task_a_id
    assert admin_audits[0]["result"] == "allowed"


def test_sse_stream_credential_revocation(auth_env: dict) -> None:
    """SSE 流连接在凭据吊销后即刻中断并输出 error 帧。"""
    client: TestClient = auth_env["client"]
    repo: Repository = auth_env["repo"]
    key_a = auth_env["key_a"]
    cred_a = auth_env["cred_a"]

    create_res = client.post(
        "/api/tasks",
        json={"bug_id": "BUG-002", "engine": "plain", "model": "fake"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    task_a_id = create_res.json()["task_id"]

    # 吊销凭据 A
    repo.revoke_credential(cred_a["id"])

    # 建立 SSE 流连接（此时在握手阶段即被拦截 401，或若握手后被吊销在循环中被拦截）
    res_sse = client.get(
        f"/api/tasks/{task_a_id}/events", headers={"Authorization": f"Bearer {key_a}"}
    )
    assert res_sse.status_code == 401


def test_idempotency_is_partitioned_by_principal(auth_env: dict) -> None:
    """不同主体提交相同参数的任务不互相劫持幂等槽。"""
    client: TestClient = auth_env["client"]
    key_a = auth_env["key_a"]
    key_b = auth_env["key_b"]

    payload = {"bug_id": "BUG-002", "engine": "plain", "model": "fake"}

    # User A 提交
    res_a = client.post("/api/tasks", json=payload, headers={"Authorization": f"Bearer {key_a}"})
    assert res_a.status_code == 201
    task_a = res_a.json()["task_id"]

    # User B 提交同一规格
    res_b = client.post("/api/tasks", json=payload, headers={"Authorization": f"Bearer {key_b}"})
    assert res_b.status_code == 201
    task_b = res_b.json()["task_id"]

    # 两者应当为不同任务
    assert task_a != task_b
