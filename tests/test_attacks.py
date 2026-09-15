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


@pytest.mark.parametrize("attack_dir", _attack_dirs())
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
    for attack_dir in _attack_dirs():
        apply_patch(ctx, (attack_dir / "attack.diff").read_text(encoding="utf-8"))
    assert working_tree_is_clean(ctx.workspace)
    assert working_tree_diff(ctx.workspace).is_empty
