"""S08:API golden path——demo/run_api_ticket.py 的三个演示段经 TestClient 验证。

与 test_demo_smoke 的分工:那边是 subprocess 直跑脚本(验证 demo/main 本身),
这边复用脚本的演示函数在 TestClient 上做全链路验证(含取消与门禁拒绝),
两者验证边界分开、不互相替代。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import demo.run_api_ticket as api_demo
from app.api.app import create_app
from app.llm.fake import FakeLLM


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "golden.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


def test_golden_path_full_flow(client: TestClient, tmp_path: Path) -> None:
    """成功工单:POST → 幂等重提 → 终态 resolved → 轨迹 → 报告 → 补丁/候选可读。"""
    summary = api_demo.run_golden_path(client)
    assert summary["status"] == "FINISHED"
    report = summary["report"]
    assert report["verdict"] == "resolved"
    assert report["validation_status"] == "passed"
    assert report["resource_status"] == "within_budget"


def test_golden_path_cancel_demo(client: TestClient, tmp_path: Path) -> None:
    """取消演示:提交后立即取消 → CANCELLED 终态(与成功任务分开)。"""
    task_id = api_demo.run_cancel_demo(client)
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "CANCELLED"
    assert task["verdict"] == "cancelled"


def test_golden_path_gate_rejection_demo(client: TestClient, tmp_path: Path) -> None:
    """门禁拒绝演示:试图改测试 → PATCH_REJECTED 终态,变更文件为空。"""
    task_id = api_demo.run_gate_rejection_demo(client)
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "PATCH_REJECTED"
    report = client.get(f"/api/tasks/{task_id}/report").json()
    assert report["changed_files"] == []  # 恶意补丁没有落盘


def test_golden_path_cancel_is_deterministic_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消演示的确定性:barrier 暂停在第一个模型调用,取消必然落在运行窗口。"""
    import app.llm.openai_client as oc
    from tests.test_live_progress import REPLAY, _service  # 复用已验证的夹具与回放
    from tests.test_service_robustness import _materialize_tiny_repo

    service = _service(tmp_path)
    repo_root = tmp_path / "tiny"
    _materialize_tiny_repo(repo_root)
    barrier, release = threading.Event(), threading.Event()

    class _BarrierFake(FakeLLM):
        def __init__(self, script: list[dict]):
            super().__init__(script)
            self._held = False

        def complete(self, messages, tools):
            if not self._held:
                self._held = True
                barrier.set()
                release.wait(timeout=60)
            return super().complete(messages, tools)

    original_build = oc.build_model
    monkeypatch.setattr(
        oc,
        "build_model",
        lambda kind, settings, script=None: (
            _BarrierFake(script or []) if script is not None else original_build(kind, settings)
        ),
    )
    try:
        task, _created = service.create_task(
            repo_path=str(repo_root),
            issue_text="cancel determinism",
            failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
            regression_tests=["tests/test_dateparse.py::test_iso_format"],
            replay_script=REPLAY,
            engine="plain",
        )
        task_id = task["task_id"]
        assert barrier.wait(timeout=30), "任务未进入运行窗口"
        assert service.cancel_task(task_id)["status"] == "CANCELLED"
        release.set()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            row = service.repo.get_task(task_id)
            if row["status"] == "CANCELLED":
                break
            time.sleep(0.1)
        assert service.repo.get_task(task_id)["status"] == "CANCELLED"
    finally:
        release.set()
        oc.build_model = original_build
        service.shutdown()
