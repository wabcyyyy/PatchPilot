"""M6 攻击案例测试:四个恶意补丁必须被对应门禁拦截,且工作区不留痕。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.gitops.testing import materialize_repo
from app.graph.gates import run_gates
from app.tools.base import ToolContext
from app.tools.patching import apply_patch
from app.tools.tracker import Tracker

BUG_ROOT = Path("bugs")
ATTACK_ROOT = BUG_ROOT / "attacks"
PYTHON = __import__("sys").executable


def _attack_dirs() -> list[Path]:
    return sorted(p for p in ATTACK_ROOT.iterdir() if p.is_dir() and p.name.startswith("ATTACK-"))


def _diff_attack_dirs() -> list[Path]:
    """补丁形态的样例(带 attack.diff);引擎级样例(如 budget)由专项测试驱动。"""
    return [d for d in _attack_dirs() if (d / "attack.diff").exists()]


def _make_ctx(target_bug: str, workspace: Path, tmp: Path) -> ToolContext:
    from app.evals.bugset import load_bug

    bug = load_bug(target_bug, BUG_ROOT)
    baseline = materialize_repo(bug.repo_dir, workspace, extra_commit=False)
    return ToolContext(
        task_id="T-ATTACK",
        workspace=workspace,
        baseline_commit=baseline,
        tracker=Tracker(tmp / "trajectory.jsonl", task_id="T-ATTACK"),
        report_dir=tmp / "reports",
        test_sets=bug.test_sets,
        allowed_paths=bug.allowed_paths,
    )


@pytest.mark.parametrize("attack_dir", _diff_attack_dirs())
def test_attack_is_blocked_by_expected_gate(attack_dir: Path, tmp_path: Path) -> None:
    meta = yaml.safe_load((attack_dir / "meta.yaml").read_text(encoding="utf-8"))
    diff_text = (attack_dir / "attack.diff").read_text(encoding="utf-8")

    ws = tmp_path / "ws"
    ctx = _make_ctx(meta["target_bug"], ws, tmp_path)
    result = apply_patch(ctx, diff_text)

    assert not result.ok, f"{meta['id']} 应被拦截"
    assert f"[{meta['expected_gate']}]" in result.error, (
        f"{meta['id']} 期望 {meta['expected_gate']} 门禁拦截,实际:{result.error}"
    )

    # 静态门禁单跑一遍也应同样判定
    gate = run_gates(diff_text, allowed_paths=ctx.allowed_paths, max_files=ctx.max_patch_files)
    assert not gate.ok


def test_attacked_workspace_stays_clean(tmp_path: Path) -> None:
    """被拒的攻击补丁不得在工作区留下任何改动。"""
    from app.gitops.differ import working_tree_diff
    from app.gitops.rollback import working_tree_is_clean

    ctx = _make_ctx("BUG-003", tmp_path / "ws", tmp_path)
    for attack_dir in _diff_attack_dirs():
        apply_patch(ctx, (attack_dir / "attack.diff").read_text(encoding="utf-8"))
    assert working_tree_is_clean(ctx.workspace)
    assert working_tree_diff(ctx.workspace).is_empty


def test_budget_attack_converges(tmp_path: Path) -> None:
    """ATTACK-008:引擎级资源门禁样例——21 步无 finish 耗尽 max_turns=20,
    必须收敛为 BUDGET_EXCEEDED 而不是无限空转或误判 resolved。"""
    import json

    from app.evals.bugset import load_bug
    from app.evals.driver import run_task
    from app.llm.fake import FakeLLM

    bug = load_bug("BUG-001", BUG_ROOT)
    steps = json.loads(
        (ATTACK_ROOT / "ATTACK-008-budget" / "replay" / "script.json").read_text(encoding="utf-8")
    )
    result = run_task(bug, FakeLLM(steps), runs_root=tmp_path / "runs")
    assert result.status == "BUDGET_EXCEEDED" and result.verdict == "failed"
