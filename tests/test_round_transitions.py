"""S04/F5:轮次语义——round_no 单点递增,拒绝后恰好还有下一轮。

缺陷现场(review F5,P1):apply 预增 round_no、route_apply 拿更新值比较,
max_rounds=2 的首轮门禁拒绝直接判耗尽,第二轮永远不发生。
修复后:拒绝与验证失败统一经 rollback(保全→reset→一次递增→耗尽终态)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evals.bugset import load_bug
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from tests.conftest import block
from tests.test_graph import BUG_ROOT, _comment_diff, _fix_diff, _localize_script

BAD_ROUND: list[dict[str, Any]] = [
    {
        "tool": "apply_patch",
        "args": {
            "patch_text": block(
                "diff --git a/tests/test_dateparse.py b/tests/test_dateparse.py\n"
                "--- a/tests/test_dateparse.py\n"
                "+++ b/tests/test_dateparse.py\n"
                "@@ -1,3 +1,4 @@\n"
                " import pytest\n"
                "+\n"
                " from src.dateparse import parse_date\n"
            )
        },
    },
    {"tool": "finish", "args": {"success": True, "summary": "试图改测试"}},
]
FIX_ROUNDS: list[dict[str, Any]] = [
    {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {"tool": "finish", "args": {"success": True, "summary": "fixed"}},
]


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_f5_max_rounds_two_first_gate_rejection_still_gets_round_two(tmp_path: Path) -> None:
    """F5 精确复现由"只有第一轮"变为恰好两轮:max_rounds=2 首拒后第二轮修好。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + BAD_ROUND + FIX_ROUNDS)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=2, task_id="T-F5")
    assert result.status == "FINISHED", (result.status, result.error)
    assert result.verdict == "resolved"
    assert result.rounds == 2, "第一轮被拒,第二轮必须真的发生"
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert names.count("apply_gate") == 2, "两轮各过一次门禁"
    # 已拒绝候选 reset 后不泄漏:changed_files 只有正确修复的文件
    assert result.changed_files == ["src/dateparse.py"]


def test_f5_both_rounds_rejected_exhausts_exactly_at_limit(tmp_path: Path) -> None:
    """两轮都拒:第三轮绝不发生,终态带 preserved 现场证据。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + BAD_ROUND + BAD_ROUND)
    result = run_task_graph(
        bug, model, runs_root=tmp_path / "runs", max_rounds=2, task_id="T-F5-EXH"
    )
    assert result.status == "BUDGET_EXCEEDED"
    assert result.verdict == "failed"
    assert "rounds exhausted after failed candidate" in (result.error or "")
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert names.count("apply_gate") == 2, "恰好两次候选尝试,第三次不执行"
    assert names.count("reset_workspace") == 2, "每轮拒绝都经 rollback 保全+复位"


def test_max_rounds_one_rejection_means_single_attempt(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + BAD_ROUND)
    result = run_task_graph(
        bug, model, runs_root=tmp_path / "runs", max_rounds=1, task_id="T-F5-ONE"
    )
    assert result.status == "BUDGET_EXCEEDED"
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert names.count("apply_gate") == 1


def test_max_rounds_three_recovers_on_last_round(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + BAD_ROUND + BAD_ROUND + FIX_ROUNDS)
    result = run_task_graph(
        bug, model, runs_root=tmp_path / "runs", max_rounds=3, task_id="T-F5-THREE"
    )
    assert result.status == "FINISHED" and result.verdict == "resolved"
    assert result.rounds == 3
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert names.count("apply_gate") == 3


def test_verify_failure_path_consumes_one_attempt_and_recovers(tmp_path: Path) -> None:
    """验证失败(非门禁)同走 rollback 单点递增:max_rounds=2 首轮验证失败、次轮修好。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    wrong_fix = [  # 打上一个"改不到点子"的补丁:门禁放行、验证失败
        {"tool": "apply_patch", "args": {"patch_text": block(_comment_diff())}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "finish", "args": {"success": True, "summary": "wrong"}},
    ]
    model = FakeLLM(_localize_script() + wrong_fix + FIX_ROUNDS)
    result = run_task_graph(
        bug, model, runs_root=tmp_path / "runs", max_rounds=2, task_id="T-F5-VERIFY"
    )
    assert result.status == "FINISHED" and result.verdict == "resolved"
    assert result.rounds == 2
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert "reset_workspace" in names, "验证失败路径的保全+复位仍由 rollback 完成"


def test_first_round_success_never_enters_rollback(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + FIX_ROUNDS)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=2)
    assert result.status == "FINISHED" and result.rounds == 1
    names = [e["tool"] for e in _events(Path(result.run_dir))]
    assert "reset_workspace" not in names and names.count("apply_gate") == 1


# ---------- 节点级:递增只发生在 rollback 一处 ----------


def _nodes_for(tmp_path: Path, max_rounds: int):
    from app.graph.nodes import TaskNodes
    from app.tools.tracker import Tracker

    return TaskNodes(
        bug=load_bug("BUG-001", BUG_ROOT),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path / "ws",
        tracker=Tracker(None, task_id="T-NODE"),
        report_dir=tmp_path / "reports",
        max_rounds=max_rounds,
        max_turns=5,
    )


def test_route_apply_sends_rejection_to_rollback_regardless_of_round(tmp_path: Path) -> None:
    nodes = _nodes_for(tmp_path, max_rounds=2)
    assert nodes.route_apply({"status": "VERIFY", "round_no": 2}) == "verify"  # type: ignore[arg-type]
    assert nodes.route_apply({"status": "PATCH_REJECTED", "round_no": 1}) == "rollback"  # type: ignore[arg-type]
    assert nodes.route_apply({"status": "PATCH_REJECTED", "round_no": 2}) == "rollback"  # type: ignore[arg-type]


def test_apply_rejection_update_no_longer_pre_increments(tmp_path: Path) -> None:
    """F5 的直接证据:apply 的拒绝返回值不含 round_no 递增(单点递增在 rollback)。

    直接把"改测试文件"写进工作区(绕过工具层,专测图级 apply 的拒绝返回):
    旧实现在这里返回 round_no+1,route 再拿更新值比较——max_rounds=2 的首轮
    拒绝直接判耗尽。
    """
    from app.gitops.testing import materialize_repo
    from app.tools.base import ToolContext

    nodes = _nodes_for(tmp_path, max_rounds=2)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    baseline = materialize_repo(load_bug("BUG-001", BUG_ROOT).repo_dir, ws, extra_commit=False)
    nodes.baseline_commit = baseline
    nodes.ctx = ToolContext(
        task_id="T-NODE",
        workspace=ws,
        baseline_commit=baseline,
        tracker=nodes.tracker,
        report_dir=tmp_path / "reports",
    )
    # 禁改测试文件是图级门禁的一部分:直接改工作区里的测试文件制造违规 diff
    test_file = ws / "tests" / "test_dateparse.py"
    cheat = "\nassert True  # cheat\n"
    test_file.write_text(
        test_file.read_text(encoding="utf-8") + cheat,
        encoding="utf-8",
        newline="\n",
    )
    update = nodes.apply({"round_no": 1, "last_feedback_signatures": [], "repeat_streak": 0})  # type: ignore[arg-type]
    assert update["status"] == "PATCH_REJECTED"
    assert "round_no" not in update, "apply 不再预增轮次"
