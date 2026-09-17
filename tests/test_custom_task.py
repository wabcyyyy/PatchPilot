"""N4a/N4b:任意仓库任务(repo_path+issue)—— 校验矩阵、e2e 与门禁攻击面。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.gitops.testing import materialize_repo

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "custom.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def custom_repo(tmp_path: Path, demo_repo: Path) -> Path:
    """物化一份演示仓库作为用户的"自有仓库"。"""
    dest = tmp_path / "my-repo"
    materialize_repo(demo_repo, dest)
    return dest


def _custom_payload(repo: Path, **overrides: object) -> dict:
    payload: dict = {
        "repo_path": str(repo),
        "issue_text": "空字符串输入时 parse_date 抛异常,应返回 None",
        "failed_tests": [FAILED_ID],
        "regression_tests": REGRESSION_IDS,
        "engine": "plain",
        "model": "fake",
        "replay_script": [{"tool": "finish", "args": {"success": True, "summary": "s"}}],
    }
    payload.update(overrides)
    return payload


# ---------- N4a:校验矩阵 ----------


def test_both_bug_id_and_repo_path_rejected(client: TestClient, custom_repo: Path) -> None:
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo, bug_id="BUG-001"))
    assert resp.status_code == 422


def test_neither_bug_id_nor_repo_path_rejected(client: TestClient) -> None:
    resp = client.post("/api/tasks", json={"engine": "plain"})
    assert resp.status_code == 422


def test_repo_path_without_required_fields_rejected(client: TestClient, custom_repo: Path) -> None:
    for missing in ("issue_text", "failed_tests", "regression_tests"):
        payload = _custom_payload(custom_repo)
        payload.pop(missing)
        resp = client.post("/api/tasks", json=payload)
        assert resp.status_code == 422, missing
        assert missing in resp.text


def test_repo_path_not_found_returns_404(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/tasks",
        json=_custom_payload(tmp_path / "no-such-dir"),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "repo_path" in body["message"]


def test_custom_fake_without_script_rejected(client: TestClient, custom_repo: Path) -> None:
    """自定义任务 + fake + 无脚本 → 404,提示二选一(补脚本或换 openai)。"""
    payload = _custom_payload(custom_repo)
    payload.pop("replay_script")
    resp = client.post("/api/tasks", json=payload)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task"
    assert "replay_script" in body["message"] and "openai" in body["message"]


def test_custom_openai_disabled_rejected(client: TestClient, custom_repo: Path) -> None:
    """自定义任务 + openai 走现有总开关前置校验(默认关闭 → 404,不建任务)。"""
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo, model="openai"))
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "PATCHPILOT_LLM_ENABLED" in body["message"]


def test_valid_custom_task_created(client: TestClient, custom_repo: Path) -> None:
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo))
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["bug_id"].startswith("CUSTOM-")
    assert task["status"] in {"QUEUED", "RUNNING"}


def test_custom_tasks_never_idempotent(client: TestClient, custom_repo: Path) -> None:
    """CUSTOM id 含随机段:同一请求两次创建是两个独立任务(正式题仍幂等,见 test_api)。"""
    first = client.post("/api/tasks", json=_custom_payload(custom_repo)).json()
    second = client.post("/api/tasks", json=_custom_payload(custom_repo)).json()
    assert first["task_id"] != second["task_id"]


# ---------- N4b:e2e 与攻击面 ----------


def wait_terminal(client: TestClient, task_id: str, timeout_s: int = 120) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.2)
    pytest.fail(f"task {task_id} did not finish in {timeout_s}s")


def _repair_script() -> list[dict]:
    """在临时副本上生成 demo_repo 的正确修复 diff,作为内存回放脚本。"""
    import shutil
    import tempfile

    from app.gitops.differ import working_tree_diff
    from app.gitops.snapshot import create_workspace

    tmp = Path(tempfile.mkdtemp(prefix="customfix-"))
    try:
        repo_src = tmp / "src"
        materialize_repo(Path(__file__).parent / "fixtures" / "demo_repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "    if value is None:\n        return None\n",
                "    if value is None:\n        return None\n    if not value.strip():\n        return None\n",
            ),
            encoding="utf-8",
            newline="\n",
        )
        diff = working_tree_diff(tmp / "ws").diff_text
        return [
            {"tool": "apply_patch", "args": {"diff_text": diff}},
            {"tool": "run_tests", "args": {"test_set": "failed"}},
            {"tool": "run_tests", "args": {"test_set": "regression"}},
            {"tool": "finish", "args": {"success": True, "summary": "空字符串早退分支已补"}},
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _evil_diff(target: str) -> str:
    return (
        f"diff --git a/{target} b/{target}\n"
        f"--- a/{target}\n"
        f"+++ b/{target}\n"
        "@@ -1,1 +1,2 @@\n"
        "+injected\n"
    )


def test_custom_task_e2e_resolved(client: TestClient, custom_repo: Path) -> None:
    """自定义仓库 + 正确修复脚本 → 全链路离线跑通,判定 resolved。"""
    resp = client.post(
        "/api/tasks", json=_custom_payload(custom_repo, replay_script=_repair_script())
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    final = wait_terminal(client, task_id)
    assert final["status"] == "FINISHED", final
    assert final["verdict"] == "resolved"

    # 三件套齐全:轨迹 / 报告 / 补丁,且报告标注 CUSTOM 来源
    run_dir = Path(final["run_dir"])
    assert (run_dir / "trajectory.jsonl").exists()
    assert (run_dir / "diff.patch").exists()
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["bug_id"].startswith("CUSTOM-") and report["verdict"] == "resolved"
    assert report["changed_files"] == ["src/dateparse.py"]

    traj = client.get(f"/api/tasks/{task_id}/trajectory?limit=100").json()
    assert traj["total_returned"] > 0
    assert {"apply_patch", "run_tests", "finish"} <= {e["tool"] for e in traj["events"]}


@pytest.mark.parametrize(
    "target",
    [
        "tests/test_dateparse.py",  # 试图修改测试文件作弊
        "../../escaped.txt",  # 试图路径穿越逃出工作区
    ],
)
def test_custom_task_malicious_patch_rejected(
    client: TestClient, custom_repo: Path, target: str
) -> None:
    """恶意脚本被门禁拦截:补丁落不了盘 → 判定 PATCH_REJECTED,现场保留。"""
    malicious = [
        {"tool": "apply_patch", "args": {"diff_text": _evil_diff(target)}},
        {"tool": "finish", "args": {"success": True, "summary": f"试图改 {target}"}},
    ]
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo, replay_script=malicious))
    assert resp.status_code == 201, resp.text
    final = wait_terminal(client, resp.json()["task_id"])
    assert final["status"] == "PATCH_REJECTED", final

    run_dir = Path(final["run_dir"])
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "failed"
    assert report["changed_files"] == []  # 恶意补丁没有落盘
    assert (run_dir / "trajectory.jsonl").exists()  # 现场保留
