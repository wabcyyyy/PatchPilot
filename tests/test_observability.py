"""测试运行指标、健康检查与可观测性 (P13)。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.logctx import JsonLogFormatter, reset_task_context, set_task_context
from app.observability.health import check_liveness, check_readiness
from app.observability.metrics import MetricsRegistry
from app.storage.repository import Repository


def test_json_log_formatter() -> None:
    tokens = set_task_context(
        task_id="task-12345",
        request_id="req-67890",
        attempt_id="att-1",
        worker_id="worker-local",
        actor="principal-alice",
    )
    try:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=20,
            msg="test message with context",
            args=(),
            exc_info=None,
        )
        record.task_id = "task-12345"
        record.request_id = "req-67890"
        record.attempt_id = "att-1"
        record.worker_id = "worker-local"
        record.actor = "principal-alice"

        formatter = JsonLogFormatter()
        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert data["level"] == "INFO"
        assert data["logger"] == "test_logger"
        assert data["message"] == "test message with context"
        assert data["task_id"] == "task-12345"
        assert data["request_id"] == "req-67890"
        assert data["attempt_id"] == "att-1"
        assert data["worker_id"] == "worker-local"
        assert data["actor"] == "principal-alice"
        assert "timestamp" in data
    finally:
        reset_task_context(tokens)


def test_check_liveness() -> None:
    res = check_liveness()
    assert res["status"] == "alive"
    assert "timestamp" in res


def test_check_readiness_healthy_and_unhealthy(tmp_path: Path) -> None:
    db_file = tmp_path / "obs_test.sqlite3"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    repo = Repository(db_file)

    # 1. 正常状态
    is_ready, details = check_readiness(
        repo=repo,
        runs_root=runs_dir,
        worker_enabled=True,
        min_free_disk_bytes=1024,
    )
    assert is_ready is True
    assert details["status"] == "ready"
    assert details["database"]["healthy"] is True
    assert details["storage"]["healthy"] is True
    assert details["queue"]["healthy"] is True

    # 2. 磁盘空间不足情况
    with patch("shutil.disk_usage") as mock_usage:
        mock_usage.return_value = type("Usage", (), {"free": 500, "total": 1000, "used": 500})()
        is_ready_disk, details_disk = check_readiness(
            repo=repo,
            runs_root=runs_dir,
            min_free_disk_bytes=10000,
        )
        assert is_ready_disk is False
        assert details_disk["status"] == "unready"
        assert details_disk["storage"]["healthy"] is False

    # 3. 仓储为空或数据库失效
    is_ready_nodb, details_nodb = check_readiness(
        repo=None,
        runs_root=runs_dir,
    )
    assert is_ready_nodb is False
    assert details_nodb["database"]["healthy"] is False


def test_metrics_registry_and_prometheus_format(tmp_path: Path) -> None:
    db_file = tmp_path / "metrics_test.sqlite3"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    repo = Repository(db_file)

    reg = MetricsRegistry.get_instance()
    reg.record_recovery_failure(2)
    reg.record_evidence_failure(1)
    reg.record_container_residual(3)
    reg.record_stage_duration("VERIFY", 4.5)

    data = reg.collect(repo, runs_dir)
    assert data["failures"]["recovery_failures_total"] >= 2
    assert data["failures"]["evidence_persistence_failures_total"] >= 1
    assert data["failures"]["container_residual_total"] >= 3
    assert "VERIFY" in data["stages"]

    rendered = reg.render_prometheus(repo, runs_dir)
    assert "# HELP patchpilot_queue_length" in rendered
    assert "patchpilot_recovery_failures_total" in rendered
    assert "patchpilot_container_residual_total" in rendered
    assert 'status="pending"' in rendered
    assert 'stage="VERIFY"' in rendered

    # 验证低基数约束：绝对禁止 task_id 出现作为 label key
    assert 'task_id="' not in rendered
    assert 'principal_id="' not in rendered


def test_api_health_and_metrics_endpoints(tmp_path: Path) -> None:
    db_file = tmp_path / "app_test.sqlite3"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    app = create_app(db_path=db_file, runs_root=runs_dir)
    with TestClient(app) as client:
        # 1. GET /api/health (向后兼容)
        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        assert r_health.json() == {"status": "ok"}

        # 2. GET /api/health/live
        r_live = client.get("/api/health/live")
        assert r_live.status_code == 200
        assert r_live.json()["status"] == "alive"

        # 3. GET /api/health/ready
        r_ready = client.get("/api/health/ready")
        assert r_ready.status_code == 200
        assert r_ready.json()["status"] == "ready"

        # 4. GET /api/metrics (Prometheus text)
        r_metrics = client.get("/api/metrics")
        assert r_metrics.status_code == 200
        assert "text/plain" in r_metrics.headers["content-type"]
        assert "patchpilot_queue_length" in r_metrics.text

        # 5. GET /api/metrics (JSON)
        r_metrics_json = client.get("/api/metrics", headers={"Accept": "application/json"})
        assert r_metrics_json.status_code == 200
        data = r_metrics_json.json()
        assert "queue" in data
        assert "tasks" in data
        assert "quota" in data
