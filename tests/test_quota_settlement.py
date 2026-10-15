"""P10-2 测试: 主体/组织额度预留、对账与结算 (Quota Reservation & Settlement)。"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import TaskService
from app.config import get_settings
from app.errors import QuotaExceededError
from app.storage.repository import Repository


def test_reserve_quota_daily_limits(tmp_path: Path):
    db_file = tmp_path / "quota.db"
    repo = Repository(db_file)

    # 1. 成功预留
    res1 = repo.reserve_quota(
        task_id="task-1",
        principal_id="user-1",
        amount_usd=5.0,
        tokens=1000,
        daily_budget_usd=10.0,
        daily_token_budget=3000,
    )
    assert res1["status"] == "reserved"
    assert res1["reserved_amount"] == 5.0
    assert res1["reserved_tokens"] == 1000

    # 2. 第二次预留仍在限额内
    res2 = repo.reserve_quota(
        task_id="task-2",
        principal_id="user-1",
        amount_usd=4.0,
        tokens=1500,
        daily_budget_usd=10.0,
        daily_token_budget=3000,
    )
    assert res2["status"] == "reserved"

    # 3. 现金超限拦截
    with pytest.raises(QuotaExceededError) as exc_info:
        repo.reserve_quota(
            task_id="task-3",
            principal_id="user-1",
            amount_usd=2.0,  # 5 + 4 + 2 = 11 > 10
            tokens=100,
            daily_budget_usd=10.0,
            daily_token_budget=3000,
        )
    assert "daily budget exceeded" in str(exc_info.value)

    # 4. Token 超限拦截
    with pytest.raises(QuotaExceededError) as exc_info2:
        repo.reserve_quota(
            task_id="task-4",
            principal_id="user-1",
            amount_usd=0.5,
            tokens=1000,  # 1000 + 1500 + 1000 = 3500 > 3000
            daily_budget_usd=10.0,
            daily_token_budget=3000,
        )
    assert "daily token budget exceeded" in str(exc_info2.value)


def test_reserve_quota_concurrent_race(tmp_path: Path):
    """并发抢占最后额度，只有一方能成功。"""
    db_file = tmp_path / "race.db"
    repo = Repository(db_file)

    # 设定每日上限 10 USD，每个任务申请 6 USD
    # 并发 4 个线程抢占，只有 1 个能成功，其余 3 个必须被 QuotaExceededError 拦截
    successes = []
    failures = []

    def _try_reserve(idx: int):
        try:
            repo.reserve_quota(
                task_id=f"race-{idx}",
                principal_id="racer",
                amount_usd=6.0,
                tokens=100,
                daily_budget_usd=10.0,
                daily_token_budget=1000,
            )
            successes.append(idx)
        except QuotaExceededError:
            failures.append(idx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_try_reserve, i) for i in range(4)]
        concurrent.futures.wait(futures)

    assert len(successes) == 1
    assert len(failures) == 3


def test_settle_and_release_quota(tmp_path: Path):
    db_file = tmp_path / "settle.db"
    repo = Repository(db_file)

    # 预留 8.0 USD, 5000 tokens
    repo.reserve_quota(
        task_id="task-settle",
        principal_id="user-s",
        amount_usd=8.0,
        tokens=5000,
        daily_budget_usd=10.0,
        daily_token_budget=10000,
    )

    # 结算实际消耗 3.5 USD, 2000 tokens
    settled = repo.settle_quota(
        task_id="task-settle",
        actual_amount_usd=3.5,
        actual_tokens=2000,
    )
    assert settled is not None
    assert settled["status"] == "settled"
    assert settled["reserved_amount"] == 3.5
    assert settled["reserved_tokens"] == 2000

    usage = repo.get_principal_quota_usage("user-s")
    assert usage["used_amount_usd"] == 3.5
    assert usage["used_tokens"] == 2000

    # 释放另一个任务配额
    repo.reserve_quota(
        task_id="task-release",
        principal_id="user-s",
        amount_usd=4.0,
        tokens=2000,
        daily_budget_usd=10.0,
        daily_token_budget=10000,
    )
    released = repo.release_quota("task-release")
    assert released is not None
    assert released["status"] == "released"
    assert released["reserved_amount"] == 0.0

    # 释放后不再计入已用额度
    usage2 = repo.get_principal_quota_usage("user-s")
    assert usage2["used_amount_usd"] == 3.5


def test_task_budget_cap_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "cap.db"
    runs_dir = tmp_path / "runs"
    repo = Repository(db_file)
    monkeypatch.setenv("PATCHPILOT_TASK_BUDGET_CAP_USD", "5.0")
    monkeypatch.setenv("PATCHPILOT_PRINCIPAL_DAILY_BUDGET_USD", "100.0")
    get_settings.cache_clear()

    service = TaskService(repo=repo, runs_root=runs_dir)

    # 请求 8.0 USD 超过单任务 5.0 USD 上限
    with pytest.raises(QuotaExceededError) as exc_info:
        service.create_task(
            bug_id="BUG-001",
            budget_usd=8.0,
        )
    assert "exceeds task cap" in str(exc_info.value)
    get_settings.cache_clear()


def test_http_quota_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "api_quota.db"
    runs_dir = tmp_path / "runs"
    repo = Repository(db_file)

    # 创建测试主体与凭据
    repo.create_principal(principal_id="p-quota", name="Quota User", role="member")
    _, raw_key = repo.issue_credential(principal_id="p-quota")

    monkeypatch.setenv("PATCHPILOT_TASK_BUDGET_CAP_USD", "5.0")
    monkeypatch.setenv("PATCHPILOT_PRINCIPAL_DAILY_BUDGET_USD", "10.0")
    get_settings.cache_clear()

    app = create_app(db_path=db_file, runs_root=runs_dir)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {raw_key}"}

        # 1. 超单任务上限 -> 429
        resp = client.post(
            "/api/tasks",
            json={"bug_id": "BUG-001", "budget_usd": 7.0},
            headers=headers,
        )
        assert resp.status_code == 429
        data = resp.json()
        assert data["code"] == "quota_exceeded"
        assert "exceeds task cap" in data["message"]
        assert resp.headers.get("Retry-After") == "60"

        # 2. 正常受理
        resp_ok = client.post(
            "/api/tasks",
            json={"bug_id": "BUG-001", "budget_usd": 4.0},
            headers=headers,
        )
        assert resp_ok.status_code in (200, 201)

        # 3. 幂等重放不双扣
        resp_replay = client.post(
            "/api/tasks",
            json={"bug_id": "BUG-001", "budget_usd": 4.0},
            headers=headers,
        )
        assert resp_replay.status_code == 200  # 命中在途幂等任务

    # 查 quota_reservations 验证只记录了一次预留
    with repo._lock:
        rows = repo._conn.execute(
            "SELECT * FROM quota_reservations WHERE principal_id = 'p-quota'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reserved_amount"] == 4.0
    get_settings.cache_clear()
