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


def _write_run(root: Path, arm: str, bug_id: str, **overrides: object) -> None:
    data = json.loads(json.dumps(REPORT_TEMPLATE))
    data["bug_id"] = bug_id
    data["task_id"] = f"T-{bug_id}-{arm}"
    data["provenance"] = dict(REPORT_TEMPLATE["provenance"])
    data["provenance"]["arm"] = arm
    for key, value in overrides.items():
        if key == "provenance_patch":
            data["provenance"].update(value)  # type: ignore[arg-type]
        else:
            data[key] = value
    run_dir = root / f"{bug_id}-{arm}"
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
    assert summary["pairs"][0]["verdicts"] == {"agent": "resolved", "one_shot": "resolved"}
    assert summary["pairs"][0]["tokens"] == {"agent": 100, "one_shot": 100}


def test_unregistered_difference_raises(tmp_path: Path) -> None:
    agent_root, one_shot_root = tmp_path / "a", tmp_path / "o"
    _write_run(agent_root, "agent", "BUG-001")
    _write_run(
        one_shot_root, "one_shot", "BUG-001", provenance_patch={"model_name": "gpt-x"}
    )
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
