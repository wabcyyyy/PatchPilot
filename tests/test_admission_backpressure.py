"""有界受理与准入控制测试 (P10-1)。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.admission import (
    AdmissionController,
    AdmissionQueueFullError,
    AdmissionTimeoutError,
    EmergencyStopError,
)
from app.api.app import create_app
from app.config import get_settings


def test_admission_controller_concurrency_and_release() -> None:
    ctrl = AdmissionController(concurrency_limit=2, queue_limit=5, timeout_seconds=2.0)
    assert ctrl.stats()["active_admissions"] == 0

    with ctrl.acquire_permit():
        assert ctrl.stats()["active_admissions"] == 1
        with ctrl.acquire_permit():
            assert ctrl.stats()["active_admissions"] == 2

    assert ctrl.stats()["active_admissions"] == 0


def test_admission_controller_queue_full_rejects_immediately() -> None:
    ctrl = AdmissionController(concurrency_limit=1, queue_limit=1, timeout_seconds=2.0)

    with ctrl.acquire_permit():
        # First waiter fills the queue
        waiter_thread_started = threading.Event()

        def _waiter() -> None:
            waiter_thread_started.set()
            with ctrl.acquire_permit():
                pass

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()
        waiter_thread_started.wait(timeout=1.0)
        time.sleep(0.05)
        assert ctrl.stats()["waiting_admissions"] == 1

        # Second waiter exceeds queue limit -> immediate rejection
        with pytest.raises(AdmissionQueueFullError) as exc_info, ctrl.acquire_permit():
            pass
        assert "queue is full" in str(exc_info.value)


def test_admission_controller_timeout_expires() -> None:
    ctrl = AdmissionController(concurrency_limit=1, queue_limit=5, timeout_seconds=0.2)

    with ctrl.acquire_permit():
        started = time.monotonic()
        with pytest.raises(AdmissionTimeoutError) as exc_info, ctrl.acquire_permit():
            pass
        elapsed = time.monotonic() - started
        assert elapsed >= 0.15
        assert "timed out" in str(exc_info.value)


def test_admission_controller_emergency_stop() -> None:
    ctrl = AdmissionController(concurrency_limit=2, queue_limit=5, emergency_stop=True)
    with pytest.raises(EmergencyStopError) as exc_info, ctrl.acquire_permit():
        pass
    assert "emergency stop is active" in str(exc_info.value)

    # Toggle off
    ctrl.set_emergency_stop(False)
    with ctrl.acquire_permit():
        assert ctrl.stats()["active_admissions"] == 1


def test_http_admission_emergency_stop_and_queue_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATCHPILOT_API_TOKEN", "test-token")
    monkeypatch.setenv("PATCHPILOT_DEPLOYMENT_MODE", "development")
    get_settings.cache_clear()

    db_path = tmp_path / "test.db"
    runs_root = tmp_path / "runs"
    app = create_app(db_path=db_path, runs_root=runs_root)

    with TestClient(app) as client:
        # Create a principal and issue credential
        repo = app.state.service.repo
        principal = repo.create_principal(principal_id="test-member", name="Member", role="member")
        _cred, raw_key = repo.issue_credential(principal_id=principal["id"])
        headers = {"Authorization": f"Bearer {raw_key}"}

        # 1. Emergency stop returns 503
        admission_ctrl: AdmissionController = app.state.admission_controller
        admission_ctrl.set_emergency_stop(True)

        try:
            resp = client.post(
                "/api/tasks",
                json={"bug_id": "BUG-001"},
                headers=headers,
            )
            assert resp.status_code == 503
            body = resp.json()
            assert body["code"] == "emergency_stop"

            # 2. Queue full returns 429 with Retry-After: 5
            admission_ctrl.set_emergency_stop(False)
            admission_ctrl.concurrency_limit = 1
            admission_ctrl.queue_limit = 0  # no queueing allowed

            # Hold the permit
            with admission_ctrl.acquire_permit():
                resp2 = client.post(
                    "/api/tasks",
                    json={"bug_id": "BUG-001"},
                    headers=headers,
                )
                assert resp2.status_code == 429
                assert resp2.headers.get("retry-after") == "5"
                body2 = resp2.json()
                assert body2["code"] == "queue_full"
        finally:
            admission_ctrl.set_emergency_stop(False)
