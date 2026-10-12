"""G12:任务事件流(SSE)的类型归类、增量续传与终态收口。

事实层是**轨迹同一张表**——所以断言分两层:
- 纯函数/生成器层:分类、帧格式、Last-Event-ID 只收其后的事件、无重复、终态帧收口;
- 路由层:真实 TestClient 读完整流(已完成任务),确认 content-type 与终态帧。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.events import classify_event, stream_task_events
from app.api.service import TaskService
from app.gitops.blockpatch import unified_to_block
from app.storage.repository import Repository
from tests.test_service_robustness import _TINY_FIX_DIFF, _materialize_tiny_repo

FAILED_ONE = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_none_returns_none",
]
FAST_REPLAY = [
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "finish", "args": {"success": True, "summary": "根因:空串未返回 None"}},
    {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_TINY_FIX_DIFF)}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {"tool": "finish", "args": {"success": True, "summary": "修复完成,回归保持通过"}},
]
TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}


def _wait(
    client: TestClient, task_id: str, predicate: Callable[[dict], bool], timeout_s: float = 240
):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if predicate(task):
            return task
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} condition not met in {timeout_s}s")


def _frames(body: str) -> list[dict]:
    """把 SSE 文本解析成 [{id, event, data}] 帧列表(协议层的最小解析器)。"""
    frames: list[dict] = []
    current: dict = {}
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(":"):
            current["heartbeat"] = True
            continue
        key, _, value = line.partition(": ")
        if key in ("id", "event", "data"):
            current[key] = value
    if current:
        frames.append(current)
    return frames


# ---------- 纯函数层 ----------


def test_classify_event_maps_the_product_surface() -> None:
    assert classify_event("tool_started") == "tool_started"
    assert classify_event("llm") == "usage"
    assert classify_event("acceptance_waiting") == "waiting"
    assert classify_event("clarification_required") == "waiting"
    assert classify_event("candidate_frozen") == "candidate"
    assert classify_event("read_file") == "tool_finished"
    assert classify_event("finish") == "tool_finished"
    assert classify_event("baseline") == "progress"
    assert classify_event("resume_from_checkpoint") == "progress"


def _service(tmp_path: Path) -> TaskService:
    return TaskService(repo=Repository(tmp_path / "db.sqlite3"), runs_root=tmp_path / "runs")


def _seed_task(service: TaskService, task_id: str, *, terminal: bool) -> None:
    service.repo.create_task(
        task_id=task_id,
        idem_key="k1",
        bug_id=task_id,
        repo_path=".",
        issue_text="x",
        max_rounds=2,
        engine="graph",
        model_provider="fake",
        run_dir=str(service.runs_root / task_id),
    )
    service.repo.insert_events(
        task_id,
        [
            {
                "event_id": f"e{index}",
                "state": "LOCALIZE",
                "tool": tool,
                "input": {"i": index},
                "output_summary": {"n": index},
                "timestamp": f"2026-10-11T00:00:0{index}",
            }
            for index, tool in enumerate(["llm", "tool_started", "read_file"], start=1)
        ],
    )
    if terminal:
        assert service.repo.finalize_task(task_id, "FINISHED", "resolved")


def test_stream_flushes_backlog_then_closes_with_terminal_frame(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_task(service, "T-done", terminal=True)
    try:
        frames = _frames("".join(stream_task_events(service, "T-done", poll_seconds=0.01)))
    finally:
        service.shutdown()
    assert frames, "终态任务也要把未读事件冲净后再关流"
    events = [f for f in frames if f["event"] != "task_status"]
    assert [f["event"] for f in events] == ["usage", "tool_started", "tool_finished"]
    ids = [int(f["id"]) for f in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), "事件 id 必须单调且不重复"
    last = frames[-1]
    assert last["event"] == "task_status"
    payload = json.loads(last["data"])
    assert payload["terminal"] is True and payload["status"] == "FINISHED"


def test_resume_after_last_event_id_only_returns_later_events(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_task(service, "T-resume", terminal=True)
    try:
        all_events = service.events_since("T-resume", 0, limit=100)
        resume_after = all_events[0]["id"]
        frames = _frames(
            "".join(
                stream_task_events(service, "T-resume", after_id=resume_after, poll_seconds=0.01)
            )
        )
    finally:
        service.shutdown()
    events = [f for f in frames if f["event"] != "task_status"]
    assert len(events) == 2, "续传只应收到 after_id 之后的事件"
    assert all(int(f["id"]) > resume_after for f in events)


def test_running_task_does_not_emit_duplicates_and_ends_by_poll_bound(tmp_path: Path) -> None:
    """运行中的任务:重复轮询不得重发同一事件;max_polls 只是测试用的收口阀。"""
    service = _service(tmp_path)
    _seed_task(service, "T-running", terminal=False)
    try:
        frames = _frames(
            "".join(stream_task_events(service, "T-running", poll_seconds=0.01, max_polls=3))
        )
    finally:
        service.shutdown()
    events = [f for f in frames if f["event"] != "task_status"]
    assert [f["event"] for f in events] == ["usage", "tool_started", "tool_finished"]
    payload = json.loads(frames[-1]["data"])
    assert frames[-1]["event"] == "task_status" and payload["terminal"] is False


# ---------- 路由层 ----------


def test_events_endpoint_streams_a_finished_task_to_the_terminal_frame(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "api.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as client:
        repo = tmp_path / "repo"
        _materialize_tiny_repo(repo)
        created = client.post(
            "/api/tasks",
            json={
                "repo_path": str(repo),
                "issue_text": "空串崩溃",
                "failed_tests": [FAILED_ONE],
                "regression_tests": list(REGRESSION),
                "engine": "graph",
                "model": "fake",
                "replay_script": list(FAST_REPLAY),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        final = _wait(client, task_id, lambda t: t["status"] in TERMINAL)
        assert final["status"] == "FINISHED"

        resp = client.get(f"/api/tasks/{task_id}/events")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = _frames(resp.text)
        types = [f["event"] for f in frames]
        assert "tool_started" in types and "tool_finished" in types and "usage" in types
        assert types[-1] == "task_status"
        assert json.loads(frames[-1]["data"])["terminal"] is True
        # 与 trajectory 同一组事实:事件 data 里的 event_id 集合不超出轨迹
        traj_ids = {
            e["event_id"]
            for e in client.get(f"/api/tasks/{task_id}/trajectory?limit=1000").json()["events"]
        }
        for frame in frames:
            if frame["event"] == "task_status":
                continue
            assert json.loads(frame["data"])["event_id"] in traj_ids

        missing = client.get("/api/tasks/nope/events")
        assert missing.status_code == 404
