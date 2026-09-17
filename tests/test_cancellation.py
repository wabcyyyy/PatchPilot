"""N3:cancel 协作式中断 —— turn 边界生效,正在跑的 pytest/LLM 调用先完成。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.cancellation import CancelRegistry
from app.errors import TaskCancelled
from app.graph.plain_loop import run_plain_loop
from app.llm.fake import FakeLLM

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}


def test_registry_semantics() -> None:
    registry = CancelRegistry()
    assert registry.request_cancel("T1") is False  # 未注册 → 取消请求无处投递
    event = registry.register("T1")
    assert not event.is_set()
    assert registry.request_cancel("T1") is True
    assert event.is_set()
    assert registry.register("T1") is event  # 重复注册复用同一事件
    registry.unregister("T1")
    assert registry.request_cancel("T1") is False


def test_plain_loop_cancelled_at_turn_boundary(demo_repo: Path, tmp_path: Path) -> None:
    """Event 预先 set + 永不结束的 stub 模型:turn 开头即抛 TaskCancelled,模型不被调用。"""

    from app.gitops.snapshot import create_workspace
    from app.tools.base import ToolContext
    from app.tools.tracker import Tracker

    baseline = create_workspace(demo_repo, tmp_path / "ws")
    ctx = ToolContext(
        task_id="T-CANCEL",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "traj.jsonl", task_id="T-CANCEL"),
        report_dir=tmp_path / "reports",
        test_sets={
            "failed": ["tests/test_dateparse.py::test_empty_string_returns_none"],
            "regression": ["tests/test_dateparse.py::test_parse_iso_format"],
        },
    )

    class NeverModel:
        def complete(self, messages, tools):
            raise AssertionError("cancel 已 set,模型不应再被调用")

    event = threading.Event()
    event.set()
    with pytest.raises(TaskCancelled):
        run_plain_loop(ctx, NeverModel(), "issue", cancel_event=event)


def test_plain_loop_without_event_still_works(demo_repo: Path, tmp_path: Path) -> None:
    """回归:不传 cancel_event,行为与之前完全一致。"""
    from app.gitops.snapshot import create_workspace
    from app.tools.base import ToolContext
    from app.tools.tracker import Tracker

    baseline = create_workspace(demo_repo, tmp_path / "ws")
    ctx = ToolContext(
        task_id="T-NOCANCEL",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "traj.jsonl", task_id="T-NOCANCEL"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": ["x"], "regression": ["y"]},
    )
    outcome = run_plain_loop(
        ctx, FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "s"}}]), "issue"
    )
    assert outcome.success and outcome.finish_declared


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "cancel.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


def wait_terminal(client: TestClient, task_id: str, timeout_s: int = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.2)
    pytest.fail(f"task {task_id} did not finish in {timeout_s}s")


def test_cancel_stops_running_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """API 集成:创建任务 → cancel → 终态 CANCELLED 且不再变化。"""
    import app.llm.openai_client as oc
    from app.llm.base import AssistantTurn, ToolCall

    release = threading.Event()

    class BlockingModel:
        """第一轮 complete 阻塞,模拟一次很慢的真实 LLM 调用。"""

        provider = "fake-replay"

        def complete(self, messages, tools):
            release.wait(timeout=60)
            return AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="list_files", arguments={})],
                finish_reason="tool_calls",
                usage_tokens=1,
            )

    monkeypatch.setattr(oc, "build_model", lambda *args, **kwargs: BlockingModel())

    created = client.post(
        "/api/tasks", json={"bug_id": "BUG-003", "engine": "plain", "model": "fake"}
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    cancelled = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    release.set()  # 让阻塞中的"LLM 调用"完成,下个 turn 边界应触发中断
    final = wait_terminal(client, task_id)
    assert final["status"] == "CANCELLED"

    time.sleep(0.5)  # 终态稳定,不再漂移
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "CANCELLED"
