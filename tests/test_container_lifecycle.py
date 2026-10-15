"""执行单元生命周期闭环与容器管理测试 (P09)。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.api.cancellation import CancelRegistry
from app.executor.container_lifecycle import (
    LABEL_CONTAINER_NAME,
    LABEL_CREATED_AT,
    LABEL_KIND,
    LABEL_MANAGED_BY,
    LABEL_TASK_ID,
    LABEL_VALUE_MANAGED,
    cleanup_orphan_containers,
    get_container_labels,
    kill_and_confirm_container,
    record_container_finished,
    record_container_started,
)
from app.executor.local_runner import run_tests


def test_get_container_labels() -> None:
    flags = get_container_labels(
        task_id="task-123",
        container_name="pp-test-abc",
        kind="test",
    )
    flag_dict: dict[str, str] = {}
    for i in range(0, len(flags), 2):
        assert flags[i] == "--label"
        k, v = flags[i + 1].split("=", 1)
        flag_dict[k] = v

    assert flag_dict[LABEL_MANAGED_BY] == LABEL_VALUE_MANAGED
    assert flag_dict[LABEL_TASK_ID] == "task-123"
    assert flag_dict[LABEL_CONTAINER_NAME] == "pp-test-abc"
    assert flag_dict[LABEL_KIND] == "test"
    assert LABEL_CREATED_AT in flag_dict


def test_record_container_lifecycle(tmp_path: Path) -> None:
    meta_dir = tmp_path / "metadata"
    record_container_started(
        task_id="task-001",
        container_name="pp-cmd-123",
        kind="command",
        metadata_dir=meta_dir,
    )
    record_container_finished(
        task_id="task-001",
        container_name="pp-cmd-123",
        exit_status="0",
        metadata_dir=meta_dir,
    )

    log_file = meta_dir / "containers.jsonl"
    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["state"] == "started"
    assert lines[0]["task_id"] == "task-001"
    assert lines[0]["container_name"] == "pp-cmd-123"
    assert lines[1]["state"] == "finished"
    assert lines[1]["exit_status"] == "0"


def test_kill_and_confirm_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "inspect" in cmd:
            # First inspect returns exited
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="exited\n")
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    confirmed = kill_and_confirm_container("pp-test-target", timeout_seconds=2.0)
    assert confirmed is True
    assert any("kill" in cmd for cmd in calls)
    assert any("inspect" in cmd for cmd in calls)


def test_cleanup_orphan_containers_only_targets_patchpilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "ps" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout="pp-orphan-1\t2026-10-15\tExited (0)\npp-orphan-2\t2026-10-15\tUp 2 hours\n",
            )
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    cleaned = cleanup_orphan_containers()
    assert cleaned == ["pp-orphan-1", "pp-orphan-2"]

    # Check filter was strictly patchpilot.managed_by
    ps_call = next(c for c in calls if "ps" in c)
    assert f"label={LABEL_MANAGED_BY}={LABEL_VALUE_MANAGED}" in ps_call

    # Check docker rm was issued per container
    rm_calls = [c for c in calls if "rm" in c]
    assert len(rm_calls) == 2
    assert rm_calls[0][-1] == "pp-orphan-1"
    assert rm_calls[1][-1] == "pp-orphan-2"


def test_run_tests_cancellation(tmp_path: Path) -> None:
    cancel_event = threading.Event()
    on_kill_called = []

    def _on_kill() -> None:
        on_kill_called.append(True)

    # Trigger cancel after 100ms
    timer = threading.Timer(0.1, cancel_event.set)
    timer.start()

    code = "import time; time.sleep(10)"
    started = time.monotonic()
    result = run_tests(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        timeout_seconds=5,
        cancel_event=cancel_event,
        on_kill=_on_kill,
    )
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    assert len(on_kill_called) == 1
    assert elapsed < 3.0  # Returned well before 10s or 5s timeout


def test_cancel_registry_with_cleanup_callbacks() -> None:
    registry = CancelRegistry()
    event = registry.register("task-xyz")
    assert not event.is_set()

    cleaned = []
    registry.register_cleanup_callback("task-xyz", lambda: cleaned.append("cleaned"))

    assert registry.request_cancel("task-xyz") is True
    assert event.is_set()
    assert cleaned == ["cleaned"]

    # Registering after already cancelled invokes callback immediately
    late_cleaned = []
    registry.register_cleanup_callback("task-xyz", lambda: late_cleaned.append("late"))
    assert late_cleaned == ["late"]
