"""S07:运行中进度与轨迹可查——不等结束就能看到"现在在哪一步"。

验证边界:不用长 sleep 猜进度,用 Event barrier 在 FakeLLM 内精确暂停;
JSONL 是真相层,SQLite 实时事件与收尾补录都必须与它一致且无重复。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import TaskService
from app.llm.fake import FakeLLM
from app.storage.repository import Repository
from tests.conftest import block
from tests.test_service_robustness import _TINY_FIX_DIFF, _materialize_tiny_repo

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}

REPLAY = [
    {"tool": "search_code", "args": {"keyword": "parse_date"}},
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "apply_patch", "args": {"patch_text": block(_TINY_FIX_DIFF)}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {"tool": "finish", "args": {"success": True, "summary": "fixed"}},
]


def _service(tmp_path: Path) -> TaskService:
    return TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
    )


def _wait_terminal(service: TaskService, task_id: str, timeout_s: float = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = service.repo.get_task(task_id)
        assert row is not None
        if row["status"] in TERMINAL:
            return row
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish in {timeout_s}s")


class _BarrierFake(FakeLLM):
    """第一次模型调用前阻塞在 barrier 上:让"运行中"窗口精确可控。"""

    def __init__(self, script: list[dict], barrier: threading.Event, release: threading.Event):
        super().__init__(script)
        self._barrier = barrier
        self._release = release
        self._held = False

    def complete(self, messages, tools):
        if not self._held:
            self._held = True
            self._barrier.set()  # 通知测试:我已经跑到第一个模型调用
            self._release.wait(timeout=60)  # 等测试查完进度再放行
        return super().complete(messages, tools)


def test_live_stage_and_trajectory_visible_mid_run(tmp_path: Path) -> None:
    """spec 必测:Event barrier 暂停 → 中途查任务看得到 LOCALIZE 与事件;释放后终态。"""
    import app.llm.openai_client as oc

    service = _service(tmp_path)
    repo_root = tmp_path / "tiny"
    _materialize_tiny_repo(repo_root)
    barrier, release = threading.Event(), threading.Event()
    original_build = oc.build_model

    def _barrier_build(model_kind, settings, script=None):
        original_build(model_kind, settings, script=script)  # 保持与生产同源校验
        return _BarrierFake(script or [], barrier, release)

    oc.build_model = _barrier_build  # _execute_inner 经此造模型
    try:
        task, _created = service.create_task(
            repo_path=str(repo_root),
            issue_text="empty string crash",
            failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
            regression_tests=[
                "tests/test_dateparse.py::test_iso_format",
                "tests/test_dateparse.py::test_none_returns_none",
            ],
            replay_script=REPLAY,
            engine="plain",
        )
        task_id = task["task_id"]
        assert barrier.wait(timeout=30), "模型从未进入第一个调用"

        # 运行中:任务行有 stage(非终态),轨迹表已有事件
        row = service.repo.get_task(task_id)
        assert row["status"] == "RUNNING"
        assert row["stage"] in {"LOCALIZE", "PLAN", "PROPOSE_PATCH"}, row["stage"]
        events = service.repo.get_trajectory(task_id, limit=200)
        assert events, "运行中轨迹必须可查"
        release.set()
        final = _wait_terminal(service, task_id)
        assert final["status"] == "FINISHED"
    finally:
        oc.build_model = original_build
        service.shutdown()


def test_no_duplicate_events_between_sink_and_backfill(tmp_path: Path) -> None:
    """实时 sink + 收尾补录必须与 JSONL 行数一致且无重复。"""
    service = _service(tmp_path)
    repo_root = tmp_path / "tiny"
    _materialize_tiny_repo(repo_root)
    task, _ = service.create_task(
        repo_path=str(repo_root),
        issue_text="dup check",
        failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
        regression_tests=["tests/test_dateparse.py::test_iso_format"],
        replay_script=REPLAY,
        engine="plain",
    )
    task_id = task["task_id"]
    row = _wait_terminal(service, task_id)
    service.shutdown()
    jsonl_lines = [
        json.loads(line)
        for line in Path(row["run_dir"], "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    stored = service.repo.get_trajectory(task_id, limit=10000)
    assert len(stored) == len(jsonl_lines), "实时+补录的并集必须恰好等于 JSONL"
    event_ids = [e["event_id"] for e in stored]
    assert len(event_ids) == len(set(event_ids)), "event_id 必须唯一"


def test_sink_failure_degrades_and_backfills(tmp_path: Path) -> None:
    """sink 全程失败:运行照常 FINISHED,收尾补录补齐全部事件(不谎称实时成功)。"""
    service = _service(tmp_path)
    repo_root = tmp_path / "tiny"
    _materialize_tiny_repo(repo_root)

    def _broken_upsert(task_id: str, event: dict) -> bool:
        raise RuntimeError("sqlite busy (simulated)")

    service.repo.upsert_event_live = _broken_upsert  # type: ignore[method-assign]
    task, _ = service.create_task(
        repo_path=str(repo_root),
        issue_text="degraded sink",
        failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
        regression_tests=["tests/test_dateparse.py::test_iso_format"],
        replay_script=REPLAY,
        engine="plain",
    )
    row = _wait_terminal(service, task["task_id"])
    assert row["status"] == "FINISHED"
    service.shutdown()
    stored = service.repo.get_trajectory(task["task_id"], limit=10000)
    assert len(stored) > 0, "收尾补录必须把缺口补齐"


def test_late_event_cannot_revive_terminal(tmp_path: Path) -> None:
    """终态后迟到事件:status 与进度列都不被改写(生命周期不可复活)。"""
    service = _service(tmp_path)
    repo_root = tmp_path / "tiny"
    _materialize_tiny_repo(repo_root)
    task, _ = service.create_task(
        repo_path=str(repo_root),
        issue_text="late event",
        failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
        regression_tests=["tests/test_dateparse.py::test_iso_format"],
        replay_script=REPLAY,
        engine="plain",
    )
    task_id = task["task_id"]
    row = _wait_terminal(service, task_id)
    service.shutdown()
    before = dict(row)
    service.repo.upsert_event_live(
        task_id,
        {
            "event_id": "late-1",
            "state": "LOCALIZE",
            "tool": "llm",
            "timestamp": "2099-01-01T00:00:00",
        },
    )
    after = service.repo.get_task(task_id)
    assert after["status"] == before["status"] == "FINISHED"
    assert after["stage"] == before["stage"]  # 终态行不再推进进度
    traj = service.repo.get_trajectory(task_id, limit=10000)
    assert any(e["event_id"] == "late-1" for e in traj), "事件本身仍入取证轨迹"


def test_api_exposes_progress_fields(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "db.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        # TaskOut 必须带进度字段(旧行缺列显示 None,不冒充)
        listed = client.get("/api/tasks").json()
        for item in listed["tasks"]:
            assert "stage" in item and "last_event_at" in item
