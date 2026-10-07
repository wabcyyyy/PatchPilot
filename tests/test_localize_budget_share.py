"""LOCALIZE 段预算份额与"额度耗尽降级继续"的用例(零网络)。

钉住两件事:
1. 份额语义——定位段只拿"任务剩余额度 × share",且 share=1.0 时与旧行为逐字等价;
2. 降级语义——**任务级总额已超**仍是终点(N-5 不动),只有"段额度用完而任务级还有余量"
   才带暂定结论继续进补丁阶段。缺陷证据:runs/swe-hard-graph 里 sphinx 两题定位段烧到
   418k tokens、apply_patch 0 次,旧语义下必然 0 产出。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.errors import BudgetError
from app.gitops.snapshot import create_workspace
from app.graph.nodes import TaskNodes
from app.graph.plain_loop import LoopOutcome, run_plain_loop
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"


def _settings(**kw: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "token_budget": 200_000,
        "localize_budget_share": 0.6,
        "task_timeout_seconds": 900,
        "max_patch_files": 5,
        "test_timeout_seconds": 120,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def _nodes(monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace, tmp_path: Path) -> TaskNodes:
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: settings)
    nodes = TaskNodes(
        bug=SimpleNamespace(
            id="BUG-X",
            issue_text="空字符串应当返回 None",
            failed_tests=[FAILED_ID],
            regression_tests=[],
            test_sets={"failed": [FAILED_ID], "regression": []},
            allowed_paths=None,
            repo_dir=Path("."),
            max_rounds=5,
        ),
        model=SimpleNamespace(provider="openai", complete=lambda *a, **k: None),  # type: ignore[arg-type]
        workspace=Path("."),
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-DEG"),
        report_dir=Path("unused"),
        max_rounds=5,
        max_turns=10,
    )
    nodes.ctx = SimpleNamespace(python_exe="python", env=None, workspace=Path("."))  # type: ignore[assignment]
    return nodes


def _state(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"round_no": 1, "tokens_used": 0, "turns": 0}
    base.update(kw)
    return base


def _raise_budget(message: str, spent: int, last_content: str = "") -> LoopOutcome:
    """用作 side_effect:模拟定位循环抛出的 BudgetError(带 N-11 携带字段)。"""
    exc = BudgetError(message)
    exc.tokens_spent = spent  # type: ignore[attr-defined]
    exc.tokens_prompt = spent  # type: ignore[attr-defined]
    exc.tokens_completion = 0  # type: ignore[attr-defined]
    exc.turns = 4  # type: ignore[attr-defined]
    exc.last_content = last_content  # type: ignore[attr-defined]
    raise exc


def test_localize_gets_only_its_share_of_remaining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> LoopOutcome:
        calls.update(kwargs)
        return LoopOutcome(
            success=True,
            summary="根因在空值分支",
            turns=3,
            tokens_used=100,
            patch_applied=False,
            finish_declared=True,
        )

    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _capture)
    nodes = _nodes(monkeypatch, _settings(), tmp_path)

    out = nodes.localize(_state(tokens_used=50_000))

    # 剩余额度 = 200000 - 50000 = 150000;份额 0.6 → 90000
    assert calls["token_budget"] == 90_000
    assert out["status"] == "PROPOSE_PATCH"


def test_share_one_is_equivalent_to_previous_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """share=1.0 必须是回退开关:定位段拿整份余量,与改动前逐字同值。"""
    calls: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> LoopOutcome:
        calls.update(kwargs)
        return LoopOutcome(True, "s", 1, 0, False, True)

    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _capture)
    nodes = _nodes(monkeypatch, _settings(localize_budget_share=1.0), tmp_path)
    nodes.localize(_state(tokens_used=30_000))

    assert calls["token_budget"] == 170_000


def test_phase_share_exhaustion_continues_with_provisional_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """段额度用完但任务级还有余量 → 带着模型最后一轮的结论进补丁阶段,并留下取证事件。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget(
            "agent loop tokens 130000 exceed budget 120000",
            60_000,
            last_content="疑似 card.py 的边界",
        ),
    )
    nodes = _nodes(monkeypatch, _settings(), tmp_path)

    out = nodes.localize(_state(tokens_used=40_000))

    assert out["status"] == "PROPOSE_PATCH"
    assert out["findings"] == "疑似 card.py 的边界"
    assert "outcome" not in out
    # N-11:已烧掉的用量必须记回账本,不蒸发
    assert out["tokens_used"] == 100_000

    traj = (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8")
    assert "localize_degraded" in traj


def test_max_turns_exhaustion_uses_the_same_degrade_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """轮次耗尽与额度耗尽同一条出口——难题档 5/7 就是死在"12 轮打满却从未 finish"。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget("agent loop exceeded max_turns=10", 30_000, last_content=""),
    )
    nodes = _nodes(monkeypatch, _settings(), tmp_path)

    out = nodes.localize(_state())

    assert out["status"] == "PROPOSE_PATCH"
    assert "未在额度内收敛" in str(out["findings"])


def test_task_level_overrun_still_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """N-5 不许破:任务级总额真的超了,仍是 BUDGET_EXCEEDED 终点,不得继续 propose。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget(
            "agent loop tokens 240000 exceed budget 120000", 180_000, "半截结论"
        ),
    )
    nodes = _nodes(monkeypatch, _settings(), tmp_path)

    out = nodes.localize(_state(tokens_used=80_000))

    assert out["status"] == "BUDGET_EXCEEDED"
    assert out["outcome"] == "failed"
    assert "localize:" in str(out["error"])
    assert "findings" not in out


def test_unlimited_budget_degrades_on_turn_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """token_budget=0(不限制)时轮次耗尽同样降级,而不是判死。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget(
            "agent loop exceeded max_turns=10", 5_000, "边界在 header.py"
        ),
    )
    nodes = _nodes(monkeypatch, _settings(token_budget=0), tmp_path)

    out = nodes.localize(_state())

    assert out["status"] == "PROPOSE_PATCH"
    assert out["findings"] == "边界在 header.py"


def test_degraded_findings_carry_investigated_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """降级提示要带上"已经查过什么":实测薄 findings 会让补丁阶段重新调查而不是写补丁。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget("agent loop tokens 130000 exceed budget 120000", 60_000, ""),
    )
    nodes = _nodes(monkeypatch, _settings(), tmp_path)
    for path, times in (("sphinx/util/cfamily.py", 3), ("sphinx/domains/c.py", 1)):
        for _ in range(times):
            nodes.tracker.record(tool="read_file", state="LOCALIZE", input_payload={"path": path})
    nodes.tracker.record(tool="search_code", state="LOCALIZE", input_payload={"keyword": "family"})
    nodes.tracker.record(tool="llm", state="LOCALIZE", input_payload={"turn": 1})  # 不该进清单

    out = nodes.localize(_state(tokens_used=10_000))

    findings = str(out["findings"])
    assert "未在额度内收敛" in findings
    assert "sphinx/util/cfamily.py ×3" in findings
    assert "sphinx/domains/c.py ×1" in findings
    assert "family ×1" in findings
    assert "llm" not in findings  # 只带只读调查线索,不带循环噪声


def test_degraded_findings_caps_long_lists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """清单封顶:每个工具最多 8 项,免得降级把提示撑成第二份上下文。"""
    monkeypatch.setattr(
        "app.graph.nodes.run_plain_loop",
        lambda *a, **k: _raise_budget("agent loop exceeded max_turns=10", 20_000, "半程结论"),
    )
    nodes = _nodes(monkeypatch, _settings(), tmp_path)
    for i in range(12):
        nodes.tracker.record(
            tool="read_file", state="LOCALIZE", input_payload={"path": f"pkg/m{i}.py"}
        )

    findings = str(nodes.localize(_state())["findings"])

    assert findings.startswith("半程结论")
    assert findings.count("pkg/m") == 8


def test_plain_loop_carries_last_content_of_latest_substantive_turn(
    demo_repo: Path, tmp_path: Path
) -> None:
    """循环把"最近一次有实质文本的输出"挂在异常上;纯工具轮不覆盖它。"""
    ws = create_workspace(demo_repo, tmp_path / "ws")
    tracker = Tracker(tmp_path / "traj.jsonl", task_id="T-LC")
    ctx = ToolContext(
        task_id="T-LC",
        workspace=ws,
        baseline_commit="x",
        tracker=tracker,
        report_dir=tmp_path / "reports",
    )
    model = FakeLLM(
        [
            {"content": "我怀疑是 parse_date 的空值分支"},
            {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
            {"content": "更新结论:card 的边界处理缺失"},
            {"tool": "list_files", "args": {}},
        ]
    )

    with pytest.raises(BudgetError) as info:
        run_plain_loop(ctx, model, "issue", max_turns=4, token_budget=0)

    assert info.value.last_content == "更新结论:card 的边界处理缺失"
