"""N4a/N4b:任意仓库任务(repo_path+issue)—— 校验矩阵、e2e 与门禁攻击面。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.gitops.testing import materialize_repo
from tests.conftest import block

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
    """自定义任务 + fake + 无脚本 → 422(T12.3 校验类错误),提示二选一(补脚本或换 openai)。"""
    payload = _custom_payload(custom_repo)
    payload.pop("replay_script")
    resp = client.post("/api/tasks", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "invalid_request"
    assert "replay_script" in body["message"] and "openai" in body["message"]


def test_custom_openai_disabled_rejected(
    client: TestClient, custom_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自定义任务 + openai 走现有总开关前置校验:开关显式关闭时 404,不建任务、不烧钱。

    显式置 false(而非依赖默认):本地 .env 可能已开 ENABLED,测试绝不能真调外部模型。
    """
    from app.config import get_settings

    monkeypatch.setenv("PATCHPILOT_LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        resp = client.post("/api/tasks", json=_custom_payload(custom_repo, model="openai"))
    finally:
        get_settings.cache_clear()
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "invalid_task" and "PATCHPILOT_LLM_ENABLED" in body["message"]


def test_valid_custom_task_created(client: TestClient, custom_repo: Path) -> None:
    resp = client.post("/api/tasks", json=_custom_payload(custom_repo))
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["bug_id"].startswith("CUSTOM-")
    assert task["status"] in {"QUEUED", "RUNNING"}


# ---------- T12.2:repo_path 根白名单 ----------


def _post_with_roots(
    client: TestClient, payload: dict, value: str, monkeypatch: pytest.MonkeyPatch
):
    """设置 PATCHPILOT_ALLOWED_REPO_ROOTS 后发请求;finally 清 settings 缓存防泄漏。"""
    from app.config import get_settings

    monkeypatch.setenv("PATCHPILOT_ALLOWED_REPO_ROOTS", value)
    get_settings.cache_clear()
    try:
        return client.post("/api/tasks", json=payload)
    finally:
        get_settings.cache_clear()


def test_repo_inside_allowed_roots_created(
    client: TestClient, custom_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _post_with_roots(
        client, _custom_payload(custom_repo), str(custom_repo.parent), monkeypatch
    )
    assert resp.status_code == 201, resp.text


def test_repo_outside_allowed_roots_rejected(
    client: TestClient, custom_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _post_with_roots(
        client, _custom_payload(custom_repo), str(tmp_path / "somewhere-else"), monkeypatch
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "invalid_request" and "allowed repo roots" in body["message"]


def test_relative_root_entry_rejected(
    client: TestClient, custom_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _post_with_roots(client, _custom_payload(custom_repo), "relative/root", monkeypatch)
    assert resp.status_code == 422
    assert "absolute" in resp.json()["message"]


def test_empty_roots_unrestricted(
    client: TestClient, custom_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _post_with_roots(client, _custom_payload(custom_repo), "", monkeypatch)
    assert resp.status_code == 201, resp.text


@pytest.mark.skipif(os.name != "nt", reason="Windows 盘符大小写语义")
def test_repo_roots_case_insensitive_on_windows(
    client: TestClient, custom_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """根白名单比较经 normcase:盘符大小写不同的合法配置不得误拒(评审整改)。"""
    parent = str(custom_repo.parent)
    lowered = parent[0].lower() + parent[1:]
    assert lowered != parent  # tmp_path 返回大写盘符
    resp = _post_with_roots(client, _custom_payload(custom_repo), lowered, monkeypatch)
    assert resp.status_code == 201, resp.text


def test_custom_tasks_idempotent_by_content(client: TestClient, custom_repo: Path) -> None:
    """N-22 整改:CUSTOM id 由任务内容确定性派生——同一内容重提命中幂等
    (同键在途返回原任务),不再是"每次必新任务"的防重失效。"""
    first = client.post("/api/tasks", json=_custom_payload(custom_repo))
    second = client.post("/api/tasks", json=_custom_payload(custom_repo))
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]


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
            {"tool": "apply_patch", "args": {"patch_text": block(diff)}},
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
        {"tool": "apply_patch", "args": {"patch_text": block(_evil_diff(target))}},
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


def test_custom_bug_id_is_content_derived(tmp_path: Path) -> None:
    """N-22 整改:同内容同 id;内容差异(测试集/allowed_paths)产生不同 id。"""
    from app.evals.bugset import build_custom_bug

    kwargs = {
        "repo_path": tmp_path,
        "issue_text": "same issue",
        "failed_tests": ["tests/test_a.py::test_one", "tests/test_b.py::test_two"],
        "regression_tests": ["tests/test_c.py::test_three"],
    }
    id1 = build_custom_bug(**kwargs).id
    id2 = build_custom_bug(**kwargs).id
    assert id1 == id2 and id1.startswith("CUSTOM-")

    swapped = build_custom_bug(**{**kwargs, "failed_tests": list(reversed(kwargs["failed_tests"]))})
    assert swapped.id == id1  # 顺序无关(排序后参与散列)

    diff_content = build_custom_bug(**{**kwargs, "issue_text": "different issue"})
    assert diff_content.id != id1
    diff_tests = build_custom_bug(**{**kwargs, "failed_tests": ["tests/test_x.py::test_x"]})
    assert diff_tests.id != id1
    diff_scope = build_custom_bug(**{**kwargs, "allowed_paths": ["src/**"]})
    assert diff_scope.id != id1


# ---------- S01/F3:参数化 tuple ID 经 API 全链路 ----------

_TUPLE_SRC = (
    "def area(value):\n"
    "    w, h = value\n"
    "    if w == 1 and h == 2:\n"
    "        return 3  # baseline bug\n"
    "    return w * h\n"
)

_TUPLE_TESTS = (
    "import pytest\n"
    "from src.rect import area\n"
    "\n"
    "@pytest.mark.parametrize('size', [(1, 2), (3, 4)], ids=['(1,2)', '(3,4)'])\n"
    "def test_area(size):\n"
    "    assert area(size) == size[0] * size[1]\n"
)

_TUPLE_FIX_DIFF = (
    "--- a/src/rect.py\n"
    "+++ b/src/rect.py\n"
    "@@ -1,5 +1,3 @@\n"
    " def area(value):\n"
    "     w, h = value\n"
    "-    if w == 1 and h == 2:\n"
    "-        return 3  # baseline bug\n"
    "-    return w * h\n"
    "+    return w * h\n"
)


def test_api_accepts_parametrized_tuple_ids_and_executes_them(
    tmp_path: Path, client: TestClient
) -> None:
    """S01 验收:API 接收 `test_area[(1,2)]` 形态的合法参数化 ID,并真正只执行
    指定参数(基线红/修复后绿),verify junit 只含请求的那条参数。"""
    repo = tmp_path / "tuple-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "src" / "rect.py").write_text(_TUPLE_SRC, encoding="utf-8", newline="\n")
    (repo / "tests" / "test_area.py").write_text(_TUPLE_TESTS, encoding="utf-8", newline="\n")

    replay = [
        {"tool": "apply_patch", "args": {"patch_text": block(_TUPLE_FIX_DIFF)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "drop (1,2) special case"}},
    ]
    resp = client.post(
        "/api/tasks",
        json={
            "repo_path": str(repo),
            "issue_text": "area((1, 2)) 应为 2,基线返回 3",
            "failed_tests": ["tests/test_area.py::test_area[(1,2)]"],
            "regression_tests": ["tests/test_area.py::test_area[(3,4)]"],
            "allowed_paths": ["src/rect.py"],
            "engine": "plain",
            "model": "fake",
            "replay_script": replay,
        },
    )
    assert resp.status_code == 201, resp.text
    final = wait_terminal(client, resp.json()["task_id"])
    assert final["status"] == "FINISHED", final
    assert final["verdict"] == "resolved"

    run_dir = Path(final["run_dir"])
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["changed_files"] == ["src/rect.py"]
    # verify 只跑了请求的参数化用例:failed 集恰好 (1,2) 一条、regression 集恰好 (3,4) 一条
    failed_xml = (run_dir / "reports" / "verify-failed.xml").read_text(encoding="utf-8")
    reg_xml = (run_dir / "reports" / "verify-regression.xml").read_text(encoding="utf-8")
    assert 'name="test_area[(1,2)]"' in failed_xml
    assert "test_area[(3,4)]" not in failed_xml
    assert 'name="test_area[(3,4)]"' in reg_xml


# ---------- S05b/F4:幂等键=完整 task_spec_hash(字段边界不再裸拼接) ----------


def test_f4_split_test_groupings_no_longer_coalesce(tmp_path: Path, client: TestClient) -> None:
    """F4 核心:failed=[a],regression=[b,c] 与 failed=[a,b],regression=[c]
    是不同验收契约 → 不同幂等键 → 两个任务(旧实现折叠成同一个)。"""
    repo = tmp_path / "f4-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    base = {
        "repo_path": str(repo),
        "issue_text": "f4 grouping",
        "engine": "plain",
        "model": "fake",
        "replay_script": [{"tool": "finish", "args": {"success": True, "summary": "s"}}],
    }
    first = client.post(
        "/api/tasks",
        json={
            **base,
            "failed_tests": ["tests/test_x.py::test_a"],
            "regression_tests": ["tests/test_x.py::test_a", "tests/test_x.py::test_a"],
        },
    )
    second = client.post(
        "/api/tasks",
        json={
            **base,
            "failed_tests": ["tests/test_x.py::test_a", "tests/test_x.py::test_a"],
            "regression_tests": ["tests/test_x.py::test_a"],
        },
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["task_id"] != second.json()["task_id"]


def test_f4_different_replay_scripts_do_not_coalesce(tmp_path: Path, client: TestClient) -> None:
    """执行参数/回放不同不得 coalesce:同 repo 同 issue,不同 replay → 不同任务。"""
    repo = tmp_path / "r-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    base = {
        "repo_path": str(repo),
        "issue_text": "same issue",
        "engine": "plain",
        "model": "fake",
        "failed_tests": ["tests/test_x.py::test_a"],
        "regression_tests": ["tests/test_x.py::test_a"],
    }
    a = client.post(
        "/api/tasks",
        json={
            **base,
            "replay_script": [{"tool": "finish", "args": {"success": True, "summary": "A"}}],
        },
    )
    b = client.post(
        "/api/tasks",
        json={
            **base,
            "replay_script": [{"tool": "finish", "args": {"success": True, "summary": "B"}}],
        },
    )
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["task_id"] != b.json()["task_id"], "回放是任务身份的一部分"


def test_f4_issue_over_500_chars_rebuilt_faithfully(tmp_path: Path) -> None:
    """issue 原文完整参与身份与重建:DB 列截 500,但契约与重建不截。"""
    import time as _time

    from app.storage.repository import Repository
    from app.task_spec import TaskSpec

    repo = tmp_path / "i-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "conftest.py").write_text("", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    long_issue = "很长的缺陷描述,必须完整保留。" * 40
    assert len(long_issue) > 500
    from app.api.service import TaskService

    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=Path("bugs"),
    )
    task, _created = service.create_task(
        repo_path=str(repo),
        issue_text=long_issue,
        failed_tests=["tests/test_x.py::test_a"],
        regression_tests=["tests/test_x.py::test_a"],
        replay_script=[{"tool": "finish", "args": {"success": True, "summary": "s"}}],
        engine="plain",
    )
    deadline = _time.monotonic() + 60
    while _time.monotonic() < deadline:
        row = service.repo.get_task(task["task_id"])
        if row["status"] in TERMINAL:
            break
        _time.sleep(0.2)
    service.shutdown()
    # DB 列截 500(展示位),契约与重建不截
    assert len(row["issue_text"]) == 500
    spec = TaskSpec.read_file(Path(row["run_dir"]) / "task_spec.json")
    assert spec.issue_text == long_issue
    rebuilt = service._bug_from_row(row)
    assert rebuilt.issue_text == long_issue
