"""G10:issue-only 受理、任务契约模式与验收批准端到端。

覆盖(SPEC §7 G10 测试面):旧输入兼容;issue-only 可受理但不能直接修复成功;
平台红/绿基线校验;人工确认与 hash 竞态;批准前后 apply_patch 的放行边界;
崩溃恢复候选(等待态可续);manifest 冻结后变更拒绝。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import TaskService
from app.diagnosis.proposal import AcceptanceProposal, write_proposal
from app.errors import TaskError
from app.evals.bugset import BugTask
from app.gitops.blockpatch import unified_to_block
from app.storage.repository import Repository
from app.task_spec import (
    MODE_DIAGNOSE_THEN_REPAIR,
    MODE_REPAIR,
    TaskSpec,
    build_task_spec,
    write_task_spec_file,
)
from tests.test_service_robustness import _TINY_FIX_DIFF, _materialize_tiny_repo

FAILED_ONE = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_none_returns_none",
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

# 诊断段:读码 → 提议验收清单 → 结束调查;批准后:补丁 → 双集验证 → 收敛
DIAGNOSE_REPLAY = [
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {
        "tool": "propose_acceptance",
        "args": {
            "failed_tests": [FAILED_ONE],
            "regression_tests": list(REGRESSION),
            "rationale": "空串未按文档返回 None;其余用例保持通过",
        },
    },
    {"tool": "finish", "args": {"success": True, "summary": "根因:空串未返回 None"}},
    {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_TINY_FIX_DIFF)}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {"tool": "finish", "args": {"success": True, "summary": "修复完成,回归保持通过"}},
]

NO_PROPOSAL_REPLAY = [
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "finish", "args": {"success": True, "summary": "调查结束(未提交验收提议)"}},
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "api.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


def _wait(
    client: TestClient, task_id: str, predicate: Callable[[dict], bool], timeout_s: float = 180
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if predicate(task):
            return task
        time.sleep(0.3)
    pytest.fail(f"task {task_id} condition not met in {timeout_s}s (last={task})")


def _payload(repo: Path, replay: list[dict] | None = None) -> dict:
    """issue-only 受理载荷:repo + issue,无 failed/regression 集。"""
    return {
        "repo_path": str(repo),
        "issue_text": "parse_date('') 没有按文档返回 None,空串场景崩溃",
        "engine": "graph",
        "model": "fake",
        "replay_script": list(replay if replay is not None else DIAGNOSE_REPLAY),
    }


def _unit_bug(repo: Path, failed: list[str], regression: list[str]) -> BugTask:
    return BugTask(
        id="CUSTOM-g10-unit",
        root=repo,
        repo_dir=repo,
        issue_text="空串崩溃",
        failed_tests=list(failed),
        regression_tests=list(regression),
        max_rounds=2,
        category="custom",
    )


def test_task_spec_mode_rules_and_legacy_read(tmp_path: Path) -> None:
    """mode 契约:repair 要求双集非空、diagnose 仅 graph;旧契约缺 mode 按 repair 读。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)

    diag = build_task_spec(
        _unit_bug(repo, [], []),
        engine="graph",
        arm="agent",
        model_provider="fake",
        max_turns=20,
        max_rounds=2,
        mode=MODE_DIAGNOSE_THEN_REPAIR,
    )
    assert diag.mode == MODE_DIAGNOSE_THEN_REPAIR
    assert diag.content_dict()["mode"] == MODE_DIAGNOSE_THEN_REPAIR

    repair = build_task_spec(
        _unit_bug(repo, [FAILED_ONE], list(REGRESSION)),
        engine="graph",
        arm="agent",
        model_provider="fake",
        max_turns=20,
        max_rounds=2,
    )
    assert repair.mode == MODE_REPAIR
    assert "mode" not in repair.content_dict()  # repair 不参与身份

    # 旧契约(引入 mode 之前落盘)缺字段 → 按 repair 读,hash 与登记值同源
    legacy = repair.to_json_dict()
    assert "mode" not in legacy  # repair 契约刻意不写 mode
    assert TaskSpec.from_json_dict(legacy).task_spec_hash == repair.task_spec_hash

    # diagnose 契约的身份绑定 mode:抹掉它不再能重建同一身份
    tampered = diag.to_json_dict()
    assert tampered["mode"] == MODE_DIAGNOSE_THEN_REPAIR
    tampered.pop("mode")
    with pytest.raises(ValueError):
        TaskSpec.from_json_dict(tampered)

    bogus = dict(legacy)
    bogus["mode"] = "whatever"
    with pytest.raises(ValueError):
        TaskSpec.from_json_dict(bogus)

    with pytest.raises(TaskError):  # repair 双集为空:兜底防线
        build_task_spec(
            _unit_bug(repo, [], []),
            engine="graph",
            arm="agent",
            model_provider="fake",
            max_turns=20,
            max_rounds=2,
        )
    with pytest.raises(TaskError):  # diagnose 仅 graph 引擎
        build_task_spec(
            _unit_bug(repo, [], []),
            engine="plain",
            arm="agent",
            model_provider="fake",
            max_turns=20,
            max_rounds=2,
            mode=MODE_DIAGNOSE_THEN_REPAIR,
        )


def test_api_issue_only_intake_partial_sets_and_idempotency(
    client: TestClient, tmp_path: Path
) -> None:
    """issue-only 可受理(201);半套测试集与 plain 组合是 422;同键重提在途幂等。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)

    partial = _payload(repo)
    partial["failed_tests"] = [FAILED_ONE]  # 给一半 = 拒绝
    assert client.post("/api/tasks", json=partial).status_code == 422

    plain = _payload(repo)
    plain["engine"] = "plain"
    assert client.post("/api/tasks", json=plain).status_code == 422

    first = client.post("/api/tasks", json=_payload(repo))
    assert first.status_code == 201, first.text
    task_id = first.json()["task_id"]

    second = client.post("/api/tasks", json=_payload(repo))
    assert second.status_code == 200 and second.json()["task_id"] == task_id

    cancel = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancel.status_code == 200
    final = _wait(client, task_id, lambda t: t["status"] in TERMINAL)
    assert final["status"] == "CANCELLED"


def test_approve_endpoint_freezes_manifest_and_task_resolves(
    client: TestClient, tmp_path: Path
) -> None:
    """等待批准 → POST acceptance 冻结 manifest → 任务继续并 resolved;批准前无补丁。

    批准是冻结工件:响应带的 hash 与盘上 manifest 一致;终态后再批准被 409 拒绝
    (冻结后不可原地变更)。
    """
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    created = client.post("/api/tasks", json=_payload(repo))
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    waiting = _wait(client, task_id, lambda t: t["status"] == "WAITING_INPUT")
    run_dir = Path(waiting["run_dir"])
    proposal = json.loads((run_dir / "acceptance_proposal.json").read_text(encoding="utf-8"))
    assert proposal["baseline"]["verified"] is True
    assert not (run_dir / "acceptance_manifest.json").exists()
    assert not (run_dir / "candidates").exists()  # 批准前没有任何候选补丁

    approved = client.post(
        f"/api/tasks/{task_id}/acceptance",
        json={
            "proposal_id": proposal["proposal_id"],
            "content_hash": proposal["content_hash"],
            "expected_revision": proposal["revision"],
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["already_frozen"] is False
    assert approved.json()["manifest_hash"] == proposal["content_hash"]

    final = _wait(client, task_id, lambda t: t["status"] in TERMINAL)
    assert final["status"] == "FINISHED" and final["verdict"] == "resolved"
    report = client.get(f"/api/tasks/{task_id}/report").json()
    assert report["verdict"] == "resolved"

    manifest = json.loads((run_dir / "acceptance_manifest.json").read_text(encoding="utf-8"))
    assert manifest["approved_by"] == "trusted_user"
    assert manifest["manifest_hash"] == proposal["content_hash"]
    assert manifest["source_snapshot_hash"] == proposal["source_snapshot_hash"]

    tools = {
        e["tool"] for e in client.get(f"/api/tasks/{task_id}/trajectory?limit=500").json()["events"]
    }
    assert {"acceptance_proposal", "acceptance_baseline", "acceptance_waiting"} <= tools
    assert "acceptance_frozen" in tools

    late = client.post(
        f"/api/tasks/{task_id}/acceptance",
        json={
            "proposal_id": proposal["proposal_id"],
            "content_hash": proposal["content_hash"],
            "expected_revision": proposal["revision"],
        },
    )
    assert late.status_code == 409  # 终态后不再接受批准


def test_headless_zero_wait_returns_needs_review(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """等待窗为 0(headless):不等待,返回 needs_review 并保留提议;清单未冻结。"""
    monkeypatch.setenv("PATCHPILOT_ACCEPTANCE_WAIT_SECONDS", "0")
    from app.config import get_settings

    get_settings.cache_clear()  # fixture 建 app 时已缓存 Settings,清掉再建任务
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    created = client.post("/api/tasks", json=_payload(repo))
    assert created.status_code == 201, created.text
    final = _wait(client, created.json()["task_id"], lambda t: t["status"] in TERMINAL)
    assert final["status"] == "NEEDS_REVIEW"
    run_dir = Path(final["run_dir"])
    assert (run_dir / "acceptance_proposal.json").is_file()
    assert not (run_dir / "acceptance_manifest.json").exists()


def test_diagnosis_without_proposal_cannot_succeed(client: TestClient, tmp_path: Path) -> None:
    """没有验收提议的 issue-only 任务不能进补丁段,更不可能"变绿即 resolved"。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    created = client.post("/api/tasks", json=_payload(repo, replay=NO_PROPOSAL_REPLAY))
    assert created.status_code == 201, created.text
    final = _wait(client, created.json()["task_id"], lambda t: t["status"] in TERMINAL)
    assert final["status"] == "NEEDS_REVIEW"
    assert final["status"] != "FINISHED"
    run_dir = Path(final["run_dir"])
    assert not (run_dir / "acceptance_manifest.json").exists()


def test_cli_approve_request_shape() -> None:
    """最小 CLI 是审批端点的薄客户端:请求形状可离线断言,不碰网络。"""
    from app.agent.cli import build_request

    method, url, payload, headers = build_request(
        [
            "approve",
            "--task-id",
            "T-1",
            "--proposal-id",
            "p1",
            "--hash",
            "h1",
            "--revision",
            "0",
            "--api",
            "http://127.0.0.1:8001/",
            "--token",
            "tok",
        ]
    )
    assert method == "POST"
    assert url == "http://127.0.0.1:8001/api/tasks/T-1/acceptance"
    assert payload == {"proposal_id": "p1", "content_hash": "h1", "expected_revision": 0}
    assert headers["Authorization"] == "Bearer tok"

    method2, url2, payload2, _ = build_request(["status", "--task-id", "T-1"])
    assert method2 == "GET"
    assert url2.endswith("/api/tasks/T-1")
    assert payload2 is None


def test_waiting_diagnosis_task_is_resumable(tmp_path: Path) -> None:
    """等待批准的任务是可恢复态:盘上提议即续跑位置,而不是当僵尸判死。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    run_dir = tmp_path / "runs" / "CUSTOM-g10-wait"
    run_dir.mkdir(parents=True)
    spec = build_task_spec(
        _unit_bug(repo, [], []),
        engine="graph",
        arm="agent",
        model_provider="fake",
        max_turns=20,
        max_rounds=2,
        mode=MODE_DIAGNOSE_THEN_REPAIR,
    )
    write_task_spec_file(run_dir, spec)
    proposal = AcceptanceProposal.build(
        task_id="CUSTOM-g10-wait",
        failed_tests=[FAILED_ONE],
        regression_tests=list(REGRESSION),
        rationale="等待批准中",
        evidence_ids=[],
        source_snapshot_hash=spec.source_snapshot_hash,
        task_spec_hash=spec.task_spec_hash,
    )
    proposal.baseline = {"verified": True}
    write_proposal(run_dir, proposal)

    service = TaskService(repo=Repository(tmp_path / "db.sqlite3"), runs_root=tmp_path / "runs")
    try:
        row = {"task_id": "CUSTOM-g10-wait", "engine": "graph", "run_dir": str(run_dir)}
        assert service._resumable_row(row) is True
        (run_dir / "acceptance_proposal.json").unlink()
        assert service._resumable_row(row) is False
    finally:
        service.shutdown()
