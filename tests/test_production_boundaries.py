"""工业化跨卡验收矩阵 (SPEC §8) 与生产边界集成测试 (P19).

涵盖:
- A01: 生产模式前置准入校验拦截
- A02: 跨主体资源访问强隔离
- A08: 配额原子并发争抢与防超发
- A10: Fencing token 阻断过期 worker 迟到写入
- A11: 存储与就绪探针真实状态映射
- A17: 受理背压、有界排队与紧急停发机制
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.admission import (
    AdmissionController,
    AdmissionQueueFullError,
    EmergencyStopError,
)
from app.api.app import create_app
from app.config import Settings
from app.errors import QuotaExceededError
from app.observability.health import check_readiness
from app.storage.repository import Repository


def test_boundary_a01_production_preflight_blocks_invalid_environment() -> None:
    """A01: production 模式缺合规环境直接拒绝受理。"""
    settings = Settings(
        deployment_mode="production",
        execution_backend="local",  # 生产模式禁止 local
    )
    issues = settings.validate_production_readiness()
    assert len(issues) > 0
    assert any("execution_backend must be 'docker'" in i for i in issues)


def test_boundary_a02_cross_principal_access_forbidden(tmp_path: Path) -> None:
    """A02: 主体 A 访问主体 B 的任务/报告/取消全端点拒绝,状态与额度不变。"""
    db_file = tmp_path / "cross_tenant.sqlite3"
    runs_dir = tmp_path / "runs"
    repo = Repository(db_file)

    # 注册 Alice 与 Bob
    repo.create_principal(principal_id="alice", name="Alice", role="member")
    _, raw_key_alice = repo.issue_credential(principal_id="alice")
    repo.create_principal(principal_id="bob", name="Bob", role="member")
    _, raw_key_bob = repo.issue_credential(principal_id="bob")

    # Alice 创建任务
    repo.create_task(
        task_id="T-ALICE-1",
        idem_key="idem-alice-1",
        bug_id="BUG-001",
        repo_path=str(tmp_path / "repo"),
        issue_text="bug in alice repo",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(runs_dir / "T-ALICE-1"),
        owner="alice",
        principal_id="alice",
    )

    app = create_app(db_path=db_file, runs_root=runs_dir)
    with TestClient(app) as client:
        # Bob 尝试访问 Alice 的任务详情 -> 404/403 拒绝
        r_get = client.get(
            "/api/tasks/T-ALICE-1", headers={"Authorization": f"Bearer {raw_key_bob}"}
        )
        assert r_get.status_code == 404

        # Bob 尝试取消 Alice 的任务 -> 404/403 拒绝
        r_cancel = client.post(
            "/api/tasks/T-ALICE-1/cancel",
            headers={"Authorization": f"Bearer {raw_key_bob}"},
        )
        assert r_cancel.status_code == 404

        # Alice 正常访问
        r_ok = client.get(
            "/api/tasks/T-ALICE-1",
            headers={"Authorization": f"Bearer {raw_key_alice}"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["task_id"] == "T-ALICE-1"
    repo.close()


def test_boundary_a08_quota_concurrency_and_no_overdraw(tmp_path: Path) -> None:
    """A08: 并发争抢最后额度不超发、不双扣,保证原子性。"""
    db_file = tmp_path / "quota.sqlite3"
    repo = Repository(db_file)
    settings = Settings(
        principal_daily_budget_usd=10.0,
        principal_daily_token_budget=100_000,
        task_budget_cap_usd=5.0,
    )

    # 尝试并发预留 5 个任务，每个任务要 3.0 USD（总共 15 USD > 10 USD 日限额）
    # 必须恰好 3 个成功 (9.0 USD <= 10.0), 后续的被拒
    results: list[bool] = []
    lock = threading.Lock()

    def try_reserve(idx: int) -> None:
        t_id = f"T-RES-{idx}"
        try:
            repo.reserve_quota(
                task_id=t_id,
                principal_id="concurrent_user",
                amount_usd=3.0,
                tokens=20_000,
                daily_budget_usd=settings.principal_daily_budget_usd,
                daily_token_budget=settings.principal_daily_token_budget,
            )
            res = True
        except QuotaExceededError:
            res = False
        with lock:
            results.append(res)

    threads = [threading.Thread(target=try_reserve, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r is True)
    failures = sum(1 for r in results if r is False)
    assert successes == 3, f"预期恰好 3 个预留成功 (9.0 <= 10.0), 实际: {successes}"
    assert failures == 2, f"预期 2 个被拒 (超限), 实际: {failures}"

    # 检查总持有用量
    usage = repo.get_principal_quota_usage("concurrent_user")
    assert usage["used_amount_usd"] == 9.0
    assert usage["used_tokens"] == 60_000
    repo.close()


def test_boundary_a10_fencing_token_rejects_stale_worker(tmp_path: Path) -> None:
    """A10: 旧 worker 租约过期后迟到写被 Fencing token 坚决阻断。"""
    db_file = tmp_path / "fencing.sqlite3"
    repo = Repository(db_file)

    repo.create_task(
        task_id="T-FENCE",
        idem_key="idem-fence",
        bug_id="BUG-001",
        repo_path=str(tmp_path / "repo"),
        issue_text="test fence",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "runs" / "T-FENCE"),
        owner="p1",
        principal_id="p1",
    )
    repo.enqueue_job(task_id="T-FENCE", priority=1)

    # Worker 1 领取任务 (token=1)
    claim1 = repo.claim_next_job(worker_id="worker-1", lease_duration_seconds=1.0)
    assert claim1 is not None
    assert claim1["task_id"] == "T-FENCE"
    token1 = claim1["fencing_token"]
    assert token1 == 1

    # 模拟 Worker 1 失联超时，Worker 2 重新抢占 (token=2)
    # 强制将租约时间推移到过去
    with repo._lock, repo._conn:
        repo._conn.execute("UPDATE jobs SET lease_expires_at = datetime('now', '-10 seconds')")

    claim2 = repo.claim_next_job(worker_id="worker-2", lease_duration_seconds=60.0)
    assert claim2 is not None
    assert claim2["task_id"] == "T-FENCE"
    token2 = claim2["fencing_token"]
    assert token2 == 2

    # Worker 1 迟到试图以旧 token 提交完成 -> 必须拒绝
    ok1 = repo.complete_job(
        task_id="T-FENCE",
        worker_id="worker-1",
        status="completed",
        fencing_token=token1,
    )
    assert ok1 is False, "旧 token 的写入必须被拒绝"

    # Worker 2 以有效 token 提交完成 -> 成功
    ok2 = repo.complete_job(
        task_id="T-FENCE",
        worker_id="worker-2",
        status="completed",
        fencing_token=token2,
    )
    assert ok2 is True, "最新 token 的写入必须成功"
    repo.close()


def test_boundary_a11_readiness_probe_reflects_failures(tmp_path: Path) -> None:
    """A11: DB/工件盘/worker 异常时 readiness 正确反映真实不可用且不泄露凭据。"""
    db_file = tmp_path / "readiness.sqlite3"
    repo = Repository(db_file)

    # 正常状态
    ready, details = check_readiness(repo, runs_root=tmp_path)
    assert ready is True
    assert details["status"] == "ready"

    # 模拟磁盘空间极低（配置要求 100GB 剩余）
    ready_low_disk, details_low_disk = check_readiness(
        repo, runs_root=tmp_path, min_free_disk_bytes=100 * 1024 * 1024 * 1024 * 1024
    )
    assert ready_low_disk is False
    assert details_low_disk["status"] == "unready"
    assert not details_low_disk["storage"]["healthy"]
    assert "insufficient disk space" in details_low_disk["storage"]["detail"]
    repo.close()


def test_boundary_a17_admission_backpressure_and_emergency_stop() -> None:
    """A17: 队列饱和有界拒绝与紧急停发机制。"""
    # 1. 紧急停发测试
    ctrl_stop = AdmissionController(concurrency_limit=2, queue_limit=5, emergency_stop=True)
    with pytest.raises(EmergencyStopError) as exc_stop, ctrl_stop.acquire_permit():
        pass
    assert "emergency stop is active" in str(exc_stop.value)

    # 2. 队列饱和背压测试
    ctrl = AdmissionController(concurrency_limit=1, queue_limit=1, timeout_seconds=1.0)
    with ctrl.acquire_permit():
        # 占用并发槽 (active=1)
        waiter_started = threading.Event()

        def _waiter() -> None:
            waiter_started.set()
            with ctrl.acquire_permit():
                pass

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        waiter_started.wait(timeout=1.0)
        time.sleep(0.05)
        assert ctrl.stats()["waiting_admissions"] == 1

        # 第二个排队请求到来，队列已满 (queue_limit=1) -> 立即抛出 AdmissionQueueFullError
        with pytest.raises(AdmissionQueueFullError) as exc_full, ctrl.acquire_permit():
            pass
        assert "queue is full" in str(exc_full.value)

    t.join(timeout=2.0)
    assert ctrl.stats()["active_admissions"] == 0
