"""G16-C1:能力查询面与请求显式 profile——查询读数与受理层同源。

钉住三件事:
1. GET /api/capabilities 的每一项都来自登记处(角色表/profile 常量/受理规则),
   one_shot 如实登记为"未接线到 API"而不是能力;
2. 请求显式 agent_profile 冻结进 TaskSpec.effective_policy(并进身份 ⇒ 不 coalesce),
   不传 = Settings 默认(旧客户端语义不变);
3. arm=one_shot 在 API 面显式拒绝(HTTP 422 / 直连 InvalidRequestError),不静默按
   agent 跑;CLI submit 新任务默认 repair_v2 且显式随请求发送。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.cli import build_request
from app.agent.roles import ROLE_REGISTRY
from app.agent.runtime import RUNTIME_PROFILES
from app.api.app import create_app
from app.evals.bugset import load_bug
from app.task_spec import build_task_spec

BUG_ROOT = Path("bugs")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "cap.sqlite3", runs_root=tmp_path / "runs")
    with TestClient(app) as c:
        yield c


def test_capabilities_matches_registries(client: TestClient) -> None:
    resp = client.get("/api/capabilities")
    assert resp.status_code == 200
    caps = resp.json()
    assert set(caps["engines"]) == {"graph", "plain"}
    assert set(caps["profiles"]) == set(RUNTIME_PROFILES)
    # 恰有一个默认 profile,且 defaults 指着它(自洽,不依赖本机环境变量)
    marked = [name for name, item in caps["profiles"].items() if item["default"]]
    assert marked == [caps["defaults"]["agent_profile"]]
    # 同源:角色表逐字一致,不在这里手抄第二份
    for name, spec in ROLE_REGISTRY.items():
        entry = caps["roles"][name]
        assert entry["tools"] == list(spec.tools)
        assert entry["isolated_workspace"] == spec.isolated_workspace
    # one_shot 是登记未完成:评测 CLI 专用,API 不受理
    assert caps["arms"]["agent"]["accepted_via"] == ["api", "eval_cli"]
    assert caps["arms"]["one_shot"]["accepted_via"] == ["eval_cli"]
    # cost/hybrid 的现金闸规则可查询(与 validate_budget_policy 同一说法)
    assert any("cost_budget_micro_usd > 0" in rule for rule in caps["budget"]["rules"])


def _make_spec(repo: Path, *, profile: str | None = None):
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.repo_dir = repo
    return build_task_spec(
        bug,
        engine="plain",
        arm="agent",
        model_provider="fake-replay",
        max_turns=20,
        profile=profile,
    )


def test_explicit_profile_freezes_into_spec_and_identity(tmp_path: Path) -> None:
    from app.config import get_settings

    v2 = _make_spec(tmp_path / "v2", profile="repair_v2")
    assert v2.effective_policy["agent_runtime_profile"] == "repair_v2"
    assert v2.effective_policy["runtime_profile"]["profile_name"] == "repair_v2"

    default = _make_spec(tmp_path / "default")
    # 不传 = Settings 默认(旧客户端语义不变;当前默认 legacy_v1)
    assert default.effective_policy["agent_runtime_profile"] == get_settings().agent_runtime_profile
    # profile 进 effective_policy ⇒ 进内容身份:同题不同 profile 不 coalesce
    assert v2.task_spec_hash != default.task_spec_hash


def test_unknown_profile_rejected_at_spec_build(tmp_path: Path) -> None:
    from app.errors import TaskError

    with pytest.raises(TaskError, match="unknown agent_runtime_profile"):
        _make_spec(tmp_path / "bad", profile="repair_v3")


def test_api_accepts_explicit_profile_and_freezes_it(client: TestClient) -> None:
    resp = client.post(
        "/api/tasks",
        json={
            "bug_id": "BUG-003",
            "engine": "plain",
            "model": "fake",
            "agent_profile": "repair_v2",
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    # 契约在 create_task 内同步落盘:受理返回后立即可读(canonical JSON,无空格)
    spec = (Path(task["run_dir"]) / "task_spec.json").read_text(encoding="utf-8")
    assert '"profile_name":"repair_v2"' in spec


def test_api_rejects_one_shot_arm(client: TestClient) -> None:
    resp = client.post(
        "/api/tasks",
        json={"bug_id": "BUG-003", "engine": "plain", "model": "fake", "arm": "one_shot"},
    )
    assert resp.status_code == 422


def test_service_rejects_non_agent_arm_directly(tmp_path: Path) -> None:
    """直连 service(绕过 HTTP schema)传 one_shot 同样被受理处拒绝,不静默降级。"""
    from app.api.service import TaskService
    from app.errors import InvalidRequestError
    from app.storage.repository import Repository

    service = TaskService(
        repo=Repository(tmp_path / "svc.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=BUG_ROOT,
    )
    with pytest.raises(InvalidRequestError, match="only arm='agent'"):
        service.create_task(bug_id="BUG-003", engine="plain", model="fake", arm="one_shot")


def test_cli_submit_defaults_repair_v2_and_validates() -> None:
    method, url, payload, _ = build_request(["submit", "--bug-id", "BUG-001"])
    assert method == "POST" and url.endswith("/api/tasks")
    assert payload["agent_profile"] == "repair_v2"  # MVP 新任务默认,显式发送
    assert payload["arm"] == "agent"
    assert payload["bug_id"] == "BUG-001"

    _, _, legacy_payload, _ = build_request(
        ["submit", "--bug-id", "BUG-001", "--profile", "legacy_v1"]
    )
    assert legacy_payload["agent_profile"] == "legacy_v1"

    # 参数矛盾在客户端就地 exit 2,不打到服务端才 422
    with pytest.raises(SystemExit) as exc:
        build_request(["submit", "--repo-path", "some/path"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        build_request(["submit"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        build_request(["submit", "--repo-path", "p", "--issue", "x", "--failed-tests", "a"])
    assert exc.value.code == 2
