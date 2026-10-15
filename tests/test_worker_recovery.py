"""P12 测试: 租约心跳、恢复、Fencing Token 与停机守卫 (Lease, Recovery & Fencing)。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.storage.repository import Repository
from app.worker.worker import Worker


def test_fencing_token_rejects_outdated_worker_finalize(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "fencing.db")
    repo.create_task(
        task_id="t-fence",
        idem_key="idem-fence",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "fence"),
    )
    repo.enqueue_job(task_id="t-fence")

    # Worker 1 领取 (token=1)
    claimed1 = repo.claim_next_job(worker_id="worker-1", lease_duration_seconds=0.01)
    assert claimed1 is not None
    assert claimed1["fencing_token"] == 1

    # 租约超时后 Worker 2 接管 (token=2)
    time.sleep(0.02)
    claimed2 = repo.claim_next_job(worker_id="worker-2", lease_duration_seconds=60.0)
    assert claimed2 is not None
    assert claimed2["fencing_token"] == 2

    # 旧 Worker 1 尝试以 token=1 回写终态，必须被拦截拒绝
    rej = repo.finalize_task("t-fence", "FINISHED", "resolved", fencing_token=1)
    assert rej is False
    assert repo.get_task("t-fence")["status"] == "RUNNING"

    # 新 Worker 2 以 token=2 回写终态，成功
    ok = repo.finalize_task("t-fence", "FINISHED", "resolved", fencing_token=2)
    assert ok is True
    assert repo.get_task("t-fence")["status"] == "FINISHED"


def test_fencing_token_rejects_outdated_quota_settlement(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "quota_fence.db")
    repo.create_task(
        task_id="t-qf",
        idem_key="idem-qf",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "qf"),
    )
    repo.enqueue_job(task_id="t-qf")
    repo.reserve_quota(
        task_id="t-qf",
        principal_id="princ-1",
        amount_usd=1.0,
        tokens=1000,
    )

    # Worker 1 领取
    repo.claim_next_job(worker_id="w-1", lease_duration_seconds=0.01)
    time.sleep(0.02)
    # Worker 2 接管 (token=2)
    repo.claim_next_job(worker_id="w-2", lease_duration_seconds=60.0)

    # 旧 Worker 1 尝试结算配额，被拦截
    settled_old = repo.settle_quota(
        task_id="t-qf",
        actual_amount_usd=0.5,
        actual_tokens=500,
        fencing_token=1,
    )
    assert settled_old is None
    res = repo.get_quota_reservation("t-qf")
    assert res["status"] == "reserved"

    # 新 Worker 2 结算，成功
    settled_new = repo.settle_quota(
        task_id="t-qf",
        actual_amount_usd=0.5,
        actual_tokens=500,
        fencing_token=2,
    )
    assert settled_new is not None
    assert settled_new["status"] == "settled"


def test_renew_job_lease_fencing_token(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "renew.db")
    repo.create_task(
        task_id="t-renew",
        idem_key="idem-renew",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "renew"),
    )
    repo.enqueue_job(task_id="t-renew")
    repo.claim_next_job(worker_id="w-1", lease_duration_seconds=30.0)

    # 错误 fencing token 无法续期
    assert repo.renew_job_lease(task_id="t-renew", worker_id="w-1", fencing_token=999) is False
    # 正确 fencing token 续期成功
    assert repo.renew_job_lease(task_id="t-renew", worker_id="w-1", fencing_token=1) is True


def test_recover_stale_running_skips_active_and_pending_jobs(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "stale.db")

    # 1. 活 worker 任务: status=RUNNING, job=claimed 且租约在未来
    repo.create_task(
        task_id="t-active",
        idem_key="idem-active",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "active"),
    )
    repo.enqueue_job(task_id="t-active")
    repo.claim_next_job(worker_id="w-active", lease_duration_seconds=600.0)

    # 2. 失主任务: status=RUNNING, job=claimed 但租约已过期
    repo.create_task(
        task_id="t-abandoned",
        idem_key="idem-abandoned",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "abandoned"),
    )
    repo.enqueue_job(task_id="t-abandoned")
    repo.claim_next_job(worker_id="w-dead", lease_duration_seconds=0.01)
    time.sleep(0.02)

    # 3. 队列等待任务: status=QUEUED, job=pending
    repo.create_task(
        task_id="t-pending",
        idem_key="idem-pending",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "pending"),
    )
    repo.enqueue_job(task_id="t-pending")

    # 4. 遗留无 job 任务: status=RUNNING, 无 job
    repo.create_task(
        task_id="t-legacy",
        idem_key="idem-legacy",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "legacy"),
    )
    repo.set_status_unless_terminal("t-legacy", "RUNNING")

    # 运行恢复
    stale_list = repo.list_stale_running()
    stale_ids = {t["id"] for t in stale_list}
    assert "t-active" not in stale_ids
    assert "t-pending" not in stale_ids
    assert "t-abandoned" in stale_ids
    assert "t-legacy" in stale_ids

    recovered_keys = repo.recover_stale_running()
    assert "idem-active" not in recovered_keys
    assert "idem-pending" not in recovered_keys
    assert "idem-abandoned" in recovered_keys
    assert "idem-legacy" in recovered_keys

    # 验证状态
    assert repo.get_task("t-active")["status"] == "RUNNING"
    assert repo.get_task("t-pending")["status"] == "QUEUED"
    assert repo.get_task("t-abandoned")["status"] == "NEEDS_REVIEW"
    assert repo.get_task("t-legacy")["status"] == "NEEDS_REVIEW"


def test_heartbeat_lease_loss_triggers_cancellation(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "hb_cancel.db")
    repo.create_task(
        task_id="t-hb",
        idem_key="idem-hb",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="test",
        max_rounds=1,
        engine="plain",
        model_provider="fake",
        run_dir=str(tmp_path / "hb"),
    )
    repo.enqueue_job(task_id="t-hb")
    repo.claim_next_job(worker_id="w-hb", lease_duration_seconds=0.1)

    worker = Worker(
        repo=repo,
        worker_id="w-hb",
        lease_duration_seconds=0.1,
        lock_path=tmp_path / "worker.lock",
    )

    stop_event = threading.Event()
    cancel_event = threading.Event()

    # 模拟外部另一个 worker 抢占了 job 并提升 token
    now_exp = (datetime.now(UTC) + timedelta(seconds=60)).isoformat(timespec="milliseconds")
    with repo._lock, repo._conn:
        repo._conn.execute(
            "UPDATE jobs SET worker_id = 'w-other', fencing_token = 2, lease_expires_at = ? WHERE task_id = 't-hb'",
            (now_exp,),
        )

    # 启动心跳循环检测租约
    t = threading.Thread(
        target=worker._heartbeat_loop,
        args=("t-hb", 1, stop_event, cancel_event),
        daemon=True,
    )
    t.start()

    # 租约丢失应立即设置 cancel_event
    cancelled = cancel_event.wait(timeout=2.0)
    stop_event.set()
    t.join(timeout=1.0)
    assert cancelled is True
