"""P11-1 测试: 持久队列、Job 管理与原子提交 (Jobs Queue & Atomic Submission)。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.api.service import TaskService
from app.config import get_settings
from app.errors import PatchPilotError, QuotaExceededError
from app.storage.repository import Repository


def test_enqueue_and_get_job(tmp_path: Path):
    db_file = tmp_path / "jobs.db"
    repo = Repository(db_file)

    # 插入 job
    job = repo.enqueue_job(task_id="t-1", priority=10)
    assert job["task_id"] == "t-1"
    assert job["status"] == "pending"
    assert job["priority"] == 10
    assert job["fencing_token"] == 0
    assert job["worker_id"] is None

    # 查询 job
    fetched = repo.get_job(job["id"])
    assert fetched is not None
    assert fetched["id"] == job["id"]

    fetched_by_task = repo.get_job_by_task_id("t-1")
    assert fetched_by_task is not None
    assert fetched_by_task["id"] == job["id"]

    # 列表查询
    jobs_list = repo.list_jobs()
    assert len(jobs_list) == 1
    assert jobs_list[0]["id"] == job["id"]


def test_claim_and_renew_job(tmp_path: Path):
    db_file = tmp_path / "claim.db"
    repo = Repository(db_file)

    # 准备两项任务：高优先级先出
    repo.create_task(
        task_id="t-low",
        idem_key="idem-low",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="low",
        max_rounds=1,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "low"),
    )
    repo.enqueue_job(task_id="t-low", priority=0)

    repo.create_task(
        task_id="t-high",
        idem_key="idem-high",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="high",
        max_rounds=1,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "high"),
    )
    repo.enqueue_job(task_id="t-high", priority=10)

    # 领取任务：高优先级优先
    claimed = repo.claim_next_job(worker_id="w-1", lease_duration_seconds=30.0)
    assert claimed is not None
    assert claimed["task_id"] == "t-high"
    assert claimed["status"] == "claimed"
    assert claimed["worker_id"] == "w-1"
    assert claimed["fencing_token"] == 1
    assert claimed["lease_expires_at"] is not None

    # tasks 表状态联动更新为 RUNNING
    task_row = repo.get_task("t-high")
    assert task_row is not None
    assert task_row["status"] == "RUNNING"

    # 续期心跳
    renew_ok = repo.renew_job_lease(task_id="t-high", worker_id="w-1", lease_duration_seconds=60.0)
    assert renew_ok is True

    # 错误 worker 续期失败
    renew_wrong = repo.renew_job_lease(
        task_id="t-high", worker_id="w-other", lease_duration_seconds=60.0
    )
    assert renew_wrong is False

    # 完成任务
    comp_ok = repo.complete_job(task_id="t-high", worker_id="w-1", status="completed")
    assert comp_ok is True
    completed_job = repo.get_job(claimed["id"])
    assert completed_job is not None
    assert completed_job["status"] == "completed"
    assert completed_job["lease_expires_at"] is None


def test_lease_expiry_reclaim(tmp_path: Path):
    """租约过期后可被另一个 worker 重新领取，并递增 fencing_token。"""
    db_file = tmp_path / "expiry.db"
    repo = Repository(db_file)

    repo.create_task(
        task_id="t-exp",
        idem_key="idem-exp",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="exp",
        max_rounds=1,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "exp"),
    )
    repo.enqueue_job(task_id="t-exp", priority=5)

    # w-1 领取 0.1s 极短租约
    claimed1 = repo.claim_next_job(worker_id="w-1", lease_duration_seconds=0.1)
    assert claimed1 is not None
    assert claimed1["fencing_token"] == 1

    time.sleep(0.15)  # 等待租约过期

    # w-2 重新领取
    claimed2 = repo.claim_next_job(worker_id="w-2", lease_duration_seconds=30.0)
    assert claimed2 is not None
    assert claimed2["id"] == claimed1["id"]
    assert claimed2["worker_id"] == "w-2"
    assert claimed2["fencing_token"] == 2


def test_submit_task_atomic(tmp_path: Path):
    """原子提交任务：task、spec、quota 与 job 队列一次性事务化落库。"""
    db_file = tmp_path / "atomic.db"
    repo = Repository(db_file)

    task, job = repo.submit_task_atomic(
        task_id="task-atomic-1",
        idem_key="idem-atomic-1",
        bug_id="BUG-001",
        repo_path="/tmp",
        issue_text="atomic test",
        max_rounds=2,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "run-1"),
        owner="principal-a",
        principal_id="principal-a",
        spec_json='{"spec": 1}',
        schema_version=1,
        amount_usd=3.0,
        tokens=1500,
        daily_budget_usd=10.0,
        daily_token_budget=5000,
        priority=5,
    )
    assert task["id"] == "task-atomic-1"
    assert task["status"] == "QUEUED"
    assert task["task_spec_json"] == '{"spec": 1}'
    assert job["status"] == "pending"
    assert job["priority"] == 5

    # 查配额
    quota = repo.get_quota_reservation("task-atomic-1")
    assert quota is not None
    assert quota["reserved_amount"] == 3.0

    # 触发配额超限回滚测试
    with pytest.raises(QuotaExceededError):
        repo.submit_task_atomic(
            task_id="task-atomic-fail",
            idem_key="idem-atomic-fail",
            bug_id="BUG-001",
            repo_path="/tmp",
            issue_text="atomic fail",
            max_rounds=2,
            engine="graph",
            model_provider="fake",
            run_dir=str(tmp_path / "run-fail"),
            owner="principal-a",
            principal_id="principal-a",
            spec_json='{"spec": 2}',
            schema_version=1,
            amount_usd=8.0,  # 3.0 + 8.0 = 11.0 > 10.0
            tokens=100,
            daily_budget_usd=10.0,
            daily_token_budget=5000,
        )

    # 验证未有脏数据写入
    assert repo.get_task("task-atomic-fail") is None
    assert repo.get_job_by_task_id("task-atomic-fail") is None
    assert repo.get_quota_reservation("task-atomic-fail") is None

    # 触发幂等唯一约束 uq_tasks_active_principal_idem 回滚测试
    with pytest.raises(PatchPilotError) as exc_idem:
        repo.submit_task_atomic(
            task_id="task-atomic-dup",
            idem_key="idem-atomic-1",  # 与第一个任务同 key 且同 principal
            bug_id="BUG-001",
            repo_path="/tmp",
            issue_text="atomic dup",
            max_rounds=2,
            engine="graph",
            model_provider="fake",
            run_dir=str(tmp_path / "run-dup"),
            owner="principal-a",
            principal_id="principal-a",
            spec_json='{"spec": 1}',
            schema_version=1,
            amount_usd=1.0,
            tokens=100,
            daily_budget_usd=10.0,
            daily_token_budget=5000,
        )
    assert "already running" in str(exc_idem.value)
    assert repo.get_task("task-atomic-dup") is None


def test_service_worker_disabled_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """当 worker_enabled=False 时，API 服务只提交持久任务与 Job，不启动内部线程池执行。"""
    db_file = tmp_path / "worker_disabled.db"
    runs_dir = tmp_path / "runs"
    repo = Repository(db_file)

    monkeypatch.setenv("PATCHPILOT_WORKER_ENABLED", "false")
    get_settings.cache_clear()

    service = TaskService(repo=repo, runs_root=runs_dir)
    task, created = service.create_task(
        bug_id="BUG-001",
        principal_id="p-standalone",
    )
    assert created is True
    assert task["status"] == "QUEUED"

    # 验证 job 已在持久队列中等待 independent worker
    job = repo.get_job_by_task_id(task["task_id"])
    assert job is not None
    assert job["status"] == "pending"

    # 验证没有占用内部线程池执行
    assert task["task_id"] not in service._futures

    get_settings.cache_clear()
