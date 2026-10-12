"""G04:实验 manifest、去敏端点身份与 paid 驱动器 dry-run。

对应 SPEC §7 G04 的测试清单:仅 verdict 不同合法(在 test_experiment_summary 里)、
未登记漂移拒绝、三次重复都保留、去敏无凭据、dry-run 零网络/零模型请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.evals import provenance
from app.evals.experiment_manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    read_manifest,
    validate,
    write_manifest,
)

TEMPLATE = Path("docs/work-notes/experiment-manifest-template-2026-10-11.json")


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "manifest_id": "EXP-UNIT",
        "question": "单元测试用的形状,不主张任何结论",
        "engine": "graph",
        "model": {"provider": "fake-replay", "name": ""},
        "arms": [
            {"name": "agent", "variables": {}},
            {"name": "one_shot", "variables": {"PATCHPILOT_CONTEXT_WINDOW_TOKENS": "0"}},
        ],
        "bugs": ["BUG-001", "BUG-002"],
        "repetitions": 3,
        "budget": {"budget_mode": "tokens", "token_budget": 400000, "max_turns": 24},
        "stop_conditions": ["protocol_400", "spend_without_booking"],
        "authorization": {"granted_by": "offline", "scope": "zero network"},
        "outputs_root": "runs/exp-unit",
    }
    raw.update(overrides)
    return raw


def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "_run_git",
        lambda args, timeout=10: ("a" * 40 + "\n") if args[:2] == ["rev-parse", "HEAD"] else "",
    )


# ---------- manifest 校验 ----------


def test_minimal_manifest_validates_and_is_offline() -> None:
    manifest = validate(_raw())
    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
    assert manifest.arm_names() == ["agent", "one_shot"]


@pytest.mark.parametrize(
    "field",
    [
        "manifest_id",
        "question",
        "arms",
        "bugs",
        "repetitions",
        "budget",
        "stop_conditions",
        "authorization",
        "outputs_root",
        "engine",
        "model",
    ],
)
def test_every_required_field_is_enforced(field: str) -> None:
    raw = _raw()
    raw.pop(field)
    with pytest.raises(ManifestError, match=field):
        validate(raw)


def test_unregistered_top_level_field_is_rejected() -> None:
    """新字段必须先进 REQUIRED/OPTIONAL,否则"多写一个键"就悄悄改变了批的定义。"""
    with pytest.raises(ManifestError, match="未登记字段"):
        validate(_raw(some_new_knob="1"))


def test_empty_or_duplicated_inputs_rejected() -> None:
    with pytest.raises(ManifestError, match="不能为空"):
        validate(_raw(bugs=[]))
    with pytest.raises(ManifestError, match="重复"):
        validate(_raw(bugs=["BUG-001", "BUG-001"]))
    with pytest.raises(ManifestError, match="重复"):
        validate(_raw(arms=[{"name": "agent"}, {"name": "agent"}]))
    with pytest.raises(ManifestError, match="repetitions"):
        validate(_raw(repetitions=0))
    with pytest.raises(ManifestError, match="stop_conditions"):
        validate(_raw(stop_conditions=[]))


def test_online_manifest_requires_authorization_and_amount() -> None:
    """SPEC §7 G17:本文件不授权任何真实调用;在线批不写清"谁批的、批多少钱"就不许起。"""
    with pytest.raises(ManifestError, match="granted_by"):
        validate(
            _raw(
                model={"provider": "openai", "name": "gpt-4o"},
                authorization={"scope": "s", "max_cost_micro_usd": 1000},
            )
        )
    with pytest.raises(ManifestError, match="max_cost_micro_usd"):
        validate(
            _raw(
                model={"provider": "openai", "name": "gpt-4o"},
                authorization={"granted_by": "u", "scope": "s"},
            )
        )
    ok = validate(
        _raw(
            model={"provider": "openai", "name": "gpt-4o"},
            authorization={"granted_by": "u", "scope": "s", "max_cost_micro_usd": 5_000_000},
        )
    )
    assert ok.model["provider"] == "openai"


def test_unknown_budget_mode_rejected() -> None:
    with pytest.raises(ManifestError, match="budget_mode"):
        validate(_raw(budget={"budget_mode": "vouchers"}))


def test_manifest_hash_ignores_timestamps_and_is_idempotent(tmp_path: Path) -> None:
    """同一份计划重复导出必须同 hash(created_at 不参与身份),否则登记面会自己漂移。"""
    first = validate(_raw(created_at="2026-10-11T00:00:00+00:00"))
    second = validate(_raw(created_at="2026-10-12T09:00:00+00:00"))
    assert first.manifest_hash == second.manifest_hash
    path = tmp_path / "exp.json"
    written_hash = write_manifest(path, first)
    assert written_hash == first.manifest_hash
    assert read_manifest(path).manifest_hash == first.manifest_hash
    assert not list(path.parent.glob("*.tmp")), "半截 tmp 不得留下"


def test_run_matrix_and_budget_envelope() -> None:
    manifest = validate(_raw())
    plan = manifest.run_matrix()
    assert len(plan) == 2 * 2 * 3  # 臂 × 题 × 重复
    assert {(entry["bug"], entry["arm"], entry["repetition"]) for entry in plan} == {
        (bug, arm, rep)
        for bug in ("BUG-001", "BUG-002")
        for arm in ("agent", "one_shot")
        for rep in (1, 2, 3)
    }
    envelope = manifest.budget_envelope()
    assert envelope["planned_runs"] == 12
    assert envelope["manifest_hash"] == manifest.manifest_hash
    assert envelope["per_run_limits"]["token_budget"] == 400000


def test_control_identity_isolates_arm_variables() -> None:
    """控制身份里只允许"臂登记的那一个变量"不同,其余逐字段相同才能配对。"""
    manifest = validate(_raw())
    agent, one_shot = manifest.arms
    left = manifest.control_identity(agent)
    right = manifest.control_identity(one_shot)
    assert left["arm_variables"] != right["arm_variables"]
    stripped = {k: v for k, v in left.items() if k != "arm_variables"}
    stripped_other = {k: v for k, v in right.items() if k != "arm_variables"}
    assert stripped == stripped_other
    assert left["manifest_hash"] == manifest.manifest_hash


def test_shipped_template_is_loadable_and_offline() -> None:
    """模板本身必须能校验通过:模板坏了,下一个实验就会在起跑线上才发现。"""
    assert TEMPLATE.is_file(), "新实验模板必须随仓库分发"
    manifest = read_manifest(TEMPLATE)
    assert manifest.model["provider"] != "openai", "模板不得预填在线授权"
    assert manifest.repetitions >= 2, "模板应展示重复批的最小形态(n=1 不算机制证据)"


# ---------- provenance 的去敏与控制身份 ----------


def test_endpoint_identity_strips_credentials_and_keeps_a_hash() -> None:
    identity = provenance.endpoint_identity(
        "https://user:sk-live-secret@api.example.com:8443/v1?token=abc123"
    )
    blob = json.dumps(identity)
    assert "sk-live-secret" not in blob and "abc123" not in blob and "user" not in blob
    assert identity["host"] == "api.example.com:8443"
    assert identity["path"] == "/v1"
    assert len(identity["url_sha256"]) == 16
    # query 也算端点变化:hash 必须区分,值本身不落盘
    other = provenance.endpoint_identity("https://api.example.com:8443/v1?token=zzz")
    assert other["url_sha256"] != identity["url_sha256"]


def test_endpoint_identity_absent_is_explicit() -> None:
    assert provenance.endpoint_identity("") == {
        "present": False,
        "host": "",
        "path": "",
        "url_sha256": "",
    }


def test_build_provenance_carries_control_identity_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_git(monkeypatch)
    from app.config import Settings

    settings = Settings(
        llm_base_url="https://api.example.com/v1?key=sk-live-nope",
        llm_api_key="sk-live-nope",
        llm_model="gpt-4o-mini",
        api_token="bearer-secret",
    )
    # provenance 是 `from app.config import get_settings` 进来的,必须打它自己的名字
    monkeypatch.setattr(provenance, "get_settings", lambda: settings)
    prov = provenance.build_provenance("openai", "gpt-4o-mini", "graph", "agent", max_turns=9)

    blob = json.dumps(prov, ensure_ascii=False)
    assert "sk-live-nope" not in blob, "凭据(键值或 query 里的)绝不出现在 provenance"
    assert "bearer-secret" not in blob
    assert prov["provenance_schema_version"] == provenance.PROVENANCE_SCHEMA_VERSION
    assert prov["model_identity"]["alias_proves_served_version"] is False
    assert prov["model_identity"]["served_model"] is None
    assert prov["endpoint_identity"]["host"] == "api.example.com"
    assert prov["pricing_identity"]["pricing_version"]
    assert prov["protocol_identity"]["schema_sha256"]
    assert prov["protocol_identity"]["prompts_sha256"]
    assert prov["container_identity"]["digest_status"] == "not_applicable"
    assert prov["budget_identity"]["budget_mode"] == settings.budget_mode


def test_protocol_identity_records_arm_derived_fields_only_for_that_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_git(monkeypatch)
    from app.evals.experiment_policy import policy_for

    one_shot = provenance.protocol_identity("one_shot", policy_for("one_shot"))
    assert one_shot["arm"] == "one_shot"
    assert one_shot["experiment_policy"]["allow_branching"] is False
    assert one_shot["experiment_policy_sha256"]
    plain = provenance.protocol_identity("agent")
    assert plain["experiment_policy"] is None  # 不伪造一份"看起来注册过"的策略


def test_reclassified_keys_are_no_longer_exempt() -> None:
    """SPEC:文本裁剪/输出上限不是复现豁免。移动必须双向生效:进快照、离开豁免。"""
    from app.evals.provenance import EXEMPT_KEYS, RECLASSIFIED_TO_SNAPSHOT, SNAPSHOT_KEYS

    for key in RECLASSIFIED_TO_SNAPSHOT:
        assert key in SNAPSHOT_KEYS, key
        assert key not in EXEMPT_KEYS, key


def test_manifest_hash_records_identity_of_both_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """比较摘要带 manifest_hash:事后能对账"当时比的是哪两份产物、按哪份登记"。"""
    from scripts.compare_experiments import compare_batches

    _stub_git(monkeypatch)
    manifest = validate(_raw(arms=[{"name": "agent"}, {"name": "one_shot"}], repetitions=1))
    root = tmp_path / "a"
    other = tmp_path / "o"
    for target, arm in ((root, "agent"), (other, "one_shot")):
        run_dir = target / f"BUG-001-{arm}"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text(
            json.dumps(
                {
                    "task_id": f"T-{arm}",
                    "bug_id": "BUG-001",
                    "verdict": "resolved",
                    "status": "FINISHED",
                    "tokens_used": 10,
                    "provenance": {"arm": arm, **prov_min()},
                }
            ),
            encoding="utf-8",
        )
    summary = compare_batches(root, other, manifest=manifest, arms=("agent", "one_shot"))
    assert summary["manifest_hash"] == manifest.manifest_hash
    assert summary["runs_kept"] == 2


def prov_min() -> dict[str, Any]:
    """两次运行共享的最小控制身份(除臂之外全同)。"""
    return {
        "git_commit": "a" * 40,
        "engine": "graph",
        "model_provider": "fake-replay",
        "model_name": "",
        "max_turns": 24,
        "execution_backend": "local",
        "provenance_schema_version": provenance.PROVENANCE_SCHEMA_VERSION,
        "config_snapshot": {"context_window_tokens": 16000},
    }


# ---------- paid 驱动器:只认 manifest,dry-run 零网络 ----------


def _write_manifest(tmp_path: Path, **overrides: Any) -> Path:
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(_raw(**overrides), ensure_ascii=False), encoding="utf-8")
    return path


def test_paid_batch_dry_run_makes_no_requests_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_paid_batch as driver

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run 不许发起子进程/网络请求")

    monkeypatch.setattr(driver.subprocess, "run", _forbidden)
    monkeypatch.setattr(driver, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(driver, "LOCK", tmp_path / "batch.lock")

    code = driver.main(["--manifest", str(_write_manifest(tmp_path)), "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_hash"] == validate(_raw()).manifest_hash
    assert payload["planned_runs"] and payload["network_requests"] == 0
    assert payload["model"]["provider"] == "fake-replay"
    assert not (tmp_path / "batch.lock").exists(), "dry-run 不得创建运行锁"
    assert not (tmp_path / "ledger.jsonl").exists(), "dry-run 不得写台账"


def test_paid_batch_refuses_legacy_phase(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """旧命令静默继承 Q0 判据是最贵的一种失败:现在明确拒绝。"""
    import scripts.run_paid_batch as driver

    code = driver.main(["--manifest", str(_write_manifest(tmp_path)), "--phase", "q0"])
    assert code == 1
    assert "不再驱动运行" in capsys.readouterr().out


def test_paid_batch_requires_batch_level_max_turns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_paid_batch as driver

    budget = dict(_raw()["budget"])
    budget.pop("max_turns")
    code = driver.main(["--manifest", str(_write_manifest(tmp_path, budget=budget))])
    assert code == 1
    assert "max_turns" in capsys.readouterr().out


def test_paid_batch_rejects_bad_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_paid_batch as driver

    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert driver.main(["--manifest", str(path)]) == 1
    assert "manifest" in capsys.readouterr().out.lower()
