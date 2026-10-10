"""S10b:实验比较器的登记差异纪律与配对摘要。

compare_experiments 的契约:两臂除 arm(及派生策略描述)外任何 provenance 字段
不同都必须报错;题单不配对也报错;配对摘要可重建(逐题两臂结果/成本)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.compare_experiments import compare_batches

REPORT_TEMPLATE = {
    "task_id": "T-A",
    "bug_id": "BUG-001",
    "verdict": "resolved",
    "status": "FINISHED",
    "model_provider": "fake-replay",
    "engine": "graph",
    "rounds": 1,
    "turns": 2,
    "tokens_used": 100,
    "duration_ms": 1000,
    "changed_files": ["src/dateparse.py"],
    "gate_violations": [],
    "verify_failed_ok": True,
    "verify_regression_ok": True,
    "provenance": {
        "git_commit": "0" * 40,
        "model_provider": "fake-replay",
        "model_name": "",
        "engine": "graph",
        "arm": "agent",
        "max_turns": 20,
        "execution_backend": "local",
    },
}


def _write_run(root: Path, arm: str, bug_id: str, rep: int = 0, **overrides: object) -> None:
    """写一次运行的 report.json。

    `rep=0` 保留历史命名(单题单臂一个目录);`rep>=1` 给同一 (bug, arm) 的第 rep 次
    重复用唯一目录与唯一 task_id —— G04 要求每次运行都是可区分的运行,不是覆盖。
    """
    data = json.loads(json.dumps(REPORT_TEMPLATE))
    data["bug_id"] = bug_id
    data["task_id"] = f"T-{bug_id}-{arm}" if rep < 1 else f"T-{bug_id}-{arm}-r{rep}"
    data["provenance"] = dict(REPORT_TEMPLATE["provenance"])
    data["provenance"]["arm"] = arm
    for key, value in overrides.items():
        if key == "provenance_patch":
            data["provenance"].update(value)  # type: ignore[arg-type]
        else:
            data[key] = value
    run_dir = root / f"{bug_id}-{arm}" if rep < 1 else root / f"{bug_id}-{arm}-r{rep}"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_registered_arm_difference_is_the_only_allowed_one(tmp_path: Path) -> None:
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={"experiment_policy": {"policy": "one_shot"}},
    )
    summary = compare_batches(agent_root, one_shot_root)
    # G04 起摘要按"每次运行"列出:重复不再被 latest_per_bug 折成一条
    assert summary["pairs"][0]["verdicts"] == {"agent": ["resolved"], "one_shot": ["resolved"]}
    assert summary["pairs"][0]["tokens"] == {"agent": [100], "one_shot": [100]}
    assert summary["runs_kept"] == 2
    assert summary["pairing"] == "task_identity+arm+repetition"


def test_unregistered_difference_raises(tmp_path: Path) -> None:
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")
    _write_run(one_shot_root, "one_shot", "BUG-001", provenance_patch={"model_name": "gpt-x"})
    with pytest.raises(SystemExit, match="model_name"):
        compare_batches(agent_root, one_shot_root)


def test_unpaired_bugs_raise(tmp_path: Path) -> None:
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")
    _write_run(one_shot_root, "one_shot", "BUG-002")
    with pytest.raises(SystemExit, match="unpaired"):
        compare_batches(agent_root, one_shot_root)


def test_wrong_arm_composition_raises(tmp_path: Path) -> None:
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "one_shot", "BUG-001")  # 放错臂
    _write_run(one_shot_root, "one_shot", "BUG-001")
    with pytest.raises(SystemExit, match="only agent"):
        compare_batches(agent_root, one_shot_root)


# ---------- G04:结果与配置分开、每次重复都保留、manifest 登记变量 ----------


def _write_rep(root: Path, arm: str, bug_id: str, rep: int, **overrides: object) -> None:
    """同题同臂的第 rep 次运行:目录与 task_id 都唯一(重复之间必须是可区分的运行)。"""
    _write_run(root, arm, bug_id, rep, **overrides)


def _snap(**overrides: object) -> dict[str, object]:
    base = {"context_window_tokens": 16000, "llm_thinking": "", "llm_max_tokens": 4096}
    base.update(overrides)
    return base


def _manifest_raw(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "manifest_id": "EXP-G04-SELFTEST",
        "question": "比较器自检用:不主张任何机制结论",
        "engine": "graph",
        "model": {"provider": "fake-replay", "name": ""},
        "arms": [
            {"name": "agent", "variables": {}},
            {"name": "one_shot", "variables": {"PATCHPILOT_CONTEXT_WINDOW_TOKENS": "0"}},
        ],
        "bugs": ["BUG-001"],
        "repetitions": 1,
        "budget": {"budget_mode": "tokens", "token_budget": 400000},
        "stop_conditions": ["protocol_400", "spend_without_booking"],
        "authorization": {"granted_by": "offline-selftest", "scope": "zero network"},
        "outputs_root": "runs/exp-g04-selftest",
    }
    raw.update(overrides)
    return raw


def test_T07_same_control_different_results_is_accepted(tmp_path: Path) -> None:
    """T07:配置相同、结果不同合法,并且三次重复都留在摘要里。"""
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    for rep, verdict in ((1, "resolved"), (2, "failed"), (3, "resolved")):
        _write_rep(agent_root, "agent", "BUG-001", rep, verdict=verdict)
    for rep in (1, 2, 3):
        _write_rep(
            one_shot_root, "one_shot", "BUG-001", rep, verdict="failed", tokens_used=50 * rep
        )
    summary = compare_batches(agent_root, one_shot_root)
    pair = summary["pairs"][0]
    assert sorted(pair["verdicts"]["agent"]) == ["failed", "resolved", "resolved"]
    assert pair["repetitions"] == {"agent": 3, "one_shot": 3}
    assert summary["runs_kept"] == 6
    assert "归因" in summary["disclaimer"]


def test_T08_identical_results_with_config_drift_is_rejected(tmp_path: Path) -> None:
    """T08:结果完全相同也不能证明配置相同 —— 窗口漂移必须拒绝。"""
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001", provenance_patch={"config_snapshot": _snap()})
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={"config_snapshot": _snap(context_window_tokens=64000)},
    )
    with pytest.raises(SystemExit, match="未登记的配置漂移"):
        compare_batches(agent_root, one_shot_root)


def test_output_cap_and_thinking_drift_are_rejected(tmp_path: Path) -> None:
    """输出上限与 thinking 档位不是复现豁免:它们改变模型能写多少、请求长什么样。"""
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001", provenance_patch={"config_snapshot": _snap()})
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={"config_snapshot": _snap(llm_thinking="low")},
    )
    with pytest.raises(SystemExit, match="llm_thinking"):
        compare_batches(agent_root, one_shot_root)


def test_registered_variable_in_manifest_is_allowed(tmp_path: Path) -> None:
    """manifest 登记过的变量可以不同;协议字段按臂派生的部分也允许不同。"""
    from app.evals.experiment_manifest import validate
    from scripts.compare_experiments import _env_to_settings_key

    manifest = validate(_manifest_raw())
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(
        agent_root,
        "agent",
        "BUG-001",
        provenance_patch={
            "config_snapshot": _snap(),
            "provenance_schema_version": 2,
            "protocol_identity": {"arm": "agent", "budget_mode": "tokens"},
        },
    )
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={
            "config_snapshot": _snap(context_window_tokens=0),
            "provenance_schema_version": 2,
            "protocol_identity": {
                "arm": "one_shot",
                "budget_mode": "tokens",
                "experiment_policy": {"policy": "one_shot"},
            },
        },
    )
    summary = compare_batches(
        agent_root, one_shot_root, manifest=manifest, arms=("agent", "one_shot")
    )
    assert summary["manifest_hash"] == manifest.manifest_hash
    assert _env_to_settings_key("PATCHPILOT_CONTEXT_WINDOW_TOKENS") == "context_window_tokens"


def test_unregistered_variable_in_manifest_still_rejected(tmp_path: Path) -> None:
    """登记的变量只有那一个:换个键的漂移照样拒绝(登记纪律不是走过场)。"""
    from app.evals.experiment_manifest import validate

    manifest = validate(_manifest_raw())
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(
        agent_root,
        "agent",
        "BUG-001",
        provenance_patch={"config_snapshot": _snap(), "provenance_schema_version": 2},
    )
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={
            "config_snapshot": _snap(llm_max_tokens=8192),
            "provenance_schema_version": 2,
            "protocol_identity": {"experiment_policy": {"policy": "one_shot"}},
        },
    )
    with pytest.raises(SystemExit, match="llm_max_tokens"):
        compare_batches(agent_root, one_shot_root, manifest=manifest, arms=("agent", "one_shot"))


def test_duplicate_task_ids_are_diagnosed(tmp_path: Path) -> None:
    """同一个 (bug, arm) 里两个相同 task_id:记账出错的第一个信号,必须报出来。"""
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")
    original = (agent_root / "BUG-001-agent" / "report.json").read_text(encoding="utf-8")
    copy_dir = agent_root / "BUG-001-agent-copy"
    copy_dir.mkdir()
    (copy_dir / "report.json").write_text(original, encoding="utf-8")
    _write_run(one_shot_root, "one_shot", "BUG-001")
    with pytest.raises(SystemExit, match="duplicate run id"):
        compare_batches(agent_root, one_shot_root)


def test_missing_repetition_is_diagnosed(tmp_path: Path) -> None:
    """manifest 说跑三次、实际只有两次:次数不符即拒绝,不补跑也不剔除。"""
    from app.evals.experiment_manifest import validate

    manifest = validate(_manifest_raw(repetitions=3))
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    for rep in (1, 2):
        _write_rep(agent_root, "agent", "BUG-001", rep)
    for rep in (1, 2, 3):
        _write_rep(one_shot_root, "one_shot", "BUG-001", rep)
    with pytest.raises(SystemExit, match="repetition mismatch"):
        compare_batches(agent_root, one_shot_root, manifest=manifest, arms=("agent", "one_shot"))


def test_mixed_provenance_schema_versions_are_not_comparable(tmp_path: Path) -> None:
    """跨结构版本的批次互比:旧批没有控制身份节,读成"相同"就是假公平。"""
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")  # 旧形状:无 provenance_schema_version
    _write_run(
        one_shot_root,
        "one_shot",
        "BUG-001",
        provenance_patch={"provenance_schema_version": 2},
    )
    with pytest.raises(SystemExit, match="provenance_schema_version"):
        compare_batches(agent_root, one_shot_root)
