"""M5 PLAN 阶段的不变量对照:新节点只加了一步,别的一切都不许移动。

覆盖四条(行为用例在 tests/test_plan_stage.py,本文件复用它的脚本与桩):
1. 关闭臂的 plan 节点零模型调用、零轨迹事件;
2. 计划进 PROPOSE 是**追加式**:plan 为空时渲染逐字节不变,非空时原本文本仍在原位;
3. 计划段额度耗尽 = 降级继续(留 plan_degraded),任务级总额打穿 = BUDGET_EXCEEDED 终点;
4. 阶段白名单与额度口径(share)一字不动、verify 双跑次数不增不减、recursion_limit
   真的能容下每轮多出来的那个 superstep。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.errors import GraphRecursionError

from app.errors import BudgetError
from app.evals.bugset import load_bug
from app.graph.builder import build_graph
from app.graph.nodes import READ_TOOLS, WRITE_TOOLS, TaskNodes
from app.graph.plain_loop import LoopOutcome
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.prompts import PROPOSE_PROMPT, plan_block_for_propose
from app.tools.registry import FINISH_TOOL
from app.tools.tracker import Tracker
from tests.test_plan_stage import (
    _LOCALIZE,
    _NOOP_BLOCK,
    _fix_block,
    _initial,
    _llm_states,
    _propose,
    _scripted_pytest,
)

BUG_ROOT = Path("bugs")


def _settings(**kw: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "token_budget": 200_000,
        "localize_budget_share": 0.6,
        "plan_stage_enabled": True,
        "plan_budget_share": 0.15,
        "task_timeout_seconds": 900,
        "max_patch_files": 5,
        "test_timeout_seconds": 120,
        "context_window_tokens": 0,
        "context_keep_recent_turns": 6,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def _nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **setting_kw: object) -> TaskNodes:
    """直连 TaskNodes 的最小装配(与 tests/test_localize_budget_share.py 同形)。"""
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: _settings(**setting_kw))
    return TaskNodes(
        bug=SimpleNamespace(  # type: ignore[arg-type]
            id="BUG-X",
            issue_text="空字符串应当返回 None",
            failed_tests=[],
            regression_tests=[],
            allowed_paths=None,
            repo_dir=Path("."),
            max_rounds=5,
        ),
        model=SimpleNamespace(  # type: ignore[arg-type]
            provider="fake-replay", complete=lambda *a, **k: None
        ),
        workspace=tmp_path,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-PLAN-INV"),
        report_dir=tmp_path,
        max_rounds=5,
        max_turns=10,
        ctx=SimpleNamespace(python_exe="python", env=None, workspace=tmp_path),  # type: ignore[arg-type]
    )


def _state(**kw: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "round_no": 1,
        "tokens_used": 0,
        "turns": 0,
        "status": "PROPOSE_PATCH",
        "issue_text": "空字符串应当返回 None",
        "findings": "根因:parse_date 未处理空白字符串",
        "feedback": "",
        "plan": "",
    }
    base.update(kw)
    return base


def _raise_in_loop(
    monkeypatch: pytest.MonkeyPatch, message: str, spent: int, last_content: str
) -> None:
    """把 run_plain_loop 换成"抛 BudgetError"(计划段的两种耗尽只取决于异常携带的量)。"""

    def _raise(*args: object, **kwargs: object) -> LoopOutcome:
        raise BudgetError(
            message,
            tokens_spent=spent,
            tokens_prompt=spent,
            tokens_completion=0,
            turns=2,
            last_content=last_content,
        )

    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _raise)


def test_disabled_plan_node_never_touches_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭时:不进循环、不记事件、返回空计划,路由照常继续到 propose。"""

    def _boom(*args: object, **kwargs: object) -> LoopOutcome:
        raise AssertionError("plan_stage_enabled=False 时不得进入模型循环")

    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _boom)
    nodes = _nodes(monkeypatch, tmp_path, plan_stage_enabled=False)
    assert nodes.plan(_state()) == {"plan": ""}
    assert nodes.route_plan(_state(status="PROPOSE_PATCH")) == "continue"
    assert not (tmp_path / "trajectory.jsonl").exists()


def test_propose_prompt_plan_block_is_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan 为空 → 渲染逐字节不变;非空 → 追加式钉住块,原有文本一处不重排。"""
    nodes = _nodes(monkeypatch, tmp_path)
    state = _state(findings="f", feedback="fb", plan="")
    assert nodes._propose_prompt(state, 1) == PROPOSE_PROMPT.format(
        round_no=1, issue_text=state["issue_text"], findings="f", feedback="fb"
    )
    assert plan_block_for_propose("") == "" and plan_block_for_propose("   ") == ""

    plan = "改 src/dateparse.py 的空串分支,返回 None"
    prompt = nodes._propose_prompt(_state(plan=plan), 1)
    assert prompt.index("### 定位阶段结论") < prompt.index("### 修复计划")
    assert plan in prompt


def test_plan_degrades_and_continues_but_task_overrun_still_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """段份额耗尽 = 降级继续(留取证);任务级总额打穿 = 硬终点(N-5 不许破)。"""
    nodes = _nodes(monkeypatch, tmp_path)
    _raise_in_loop(monkeypatch, "agent loop tokens 30000 exceed budget 20000", 5_000, "暂定计划")
    out = nodes.plan(_state(tokens_used=10_000))
    assert out["status"] == "PROPOSE_PATCH" and out["plan"] == "暂定计划"
    assert "outcome" not in out and nodes.route_plan(out) == "continue"
    assert out["tokens_used"] == 15_000  # N-11:已烧的用量记回账本,不蒸发
    assert "plan_degraded" in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8")

    _raise_in_loop(monkeypatch, "agent loop tokens 300000 exceed budget 20000", 190_000, "半截计划")
    blown = nodes.plan(_state(tokens_used=20_000))
    assert blown["status"] == "BUDGET_EXCEEDED" and blown["outcome"] == "failed"
    assert "plan:" in str(blown["error"]) and nodes.route_plan(blown) == "end"
    assert "plan" not in blown


def test_stage_tool_sets_and_budget_shares_are_not_shifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCALIZE/PROPOSE 的白名单与额度口径一字不动;PLAN 只拿份额、白名单只留 finish。"""
    calls: list[dict[str, object]] = []

    def _capture(*args: object, **kwargs: object) -> LoopOutcome:
        calls.append(kwargs)
        return LoopOutcome(True, "根因在空值分支", 1, 0, False, True)

    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _capture)
    nodes = _nodes(monkeypatch, tmp_path)
    nodes.localize(_state(tokens_used=50_000))  # type: ignore[arg-type]
    nodes.plan(_state(tokens_used=50_000))  # type: ignore[arg-type]
    nodes.propose(_state(tokens_used=50_000))  # type: ignore[arg-type]

    assert [c["state_label"] for c in calls] == ["LOCALIZE", "PLAN", "PROPOSE_PATCH"]
    assert calls[0]["allowed_tools"] == READ_TOOLS and calls[2]["allowed_tools"] == WRITE_TOOLS
    assert calls[1]["allowed_tools"] == [FINISH_TOOL]
    # 剩余额度 150000:定位份额 0.6=90000,计划份额 0.15=22500,补丁整份 150000
    assert [c["token_budget"] for c in calls] == [90_000, 22_500, 150_000]


def test_verify_double_run_trigger_unchanged_by_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolved 路径仍是基线 2 + verify 2 + 复核 2:计划阶段不添一次,也不欠一次。"""
    calls = _scripted_pytest(monkeypatch, verify_green=True)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_LOCALIZE + _propose(_fix_block()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status == "FINISHED" and len(_llm_states(Path(result.run_dir))) == 6
    assert sorted(calls) == sorted(
        [
            "baseline-failed.xml",
            "baseline-regression.xml",
            "verify-failed.xml",
            "verify-regression.xml",
            "verify-failed-rerun.xml",
            "verify-regression-rerun.xml",
        ]
    )


def test_recursion_limit_covers_the_extra_plan_superstep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """满轮数重试:旧公式 4N+8 在 PLAN 加入后先把任务炸成 GraphRecursionError(非结构化终态),
    runner 的 5N+8 让它以 BUDGET_EXCEEDED 收尾。pytest 全脚本化,本用例只关涉步数。"""
    _scripted_pytest(monkeypatch, verify_green=False)
    rounds = 6
    bug = load_bug("BUG-001", BUG_ROOT)
    script = _LOCALIZE + _propose(_NOOP_BLOCK) * rounds
    nodes = TaskNodes(
        bug=bug,
        model=FakeLLM(list(script)),
        workspace=tmp_path / "ws",
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-REC"),
        report_dir=tmp_path / "reports",
        max_rounds=rounds,
        max_turns=10,
        started_monotonic=time.monotonic(),
    )
    with pytest.raises(GraphRecursionError):
        build_graph(nodes).invoke(_initial(bug, rounds), config={"recursion_limit": 4 * rounds + 8})

    result = run_task_graph(
        bug, FakeLLM(list(script)), runs_root=tmp_path / "runs2", max_rounds=rounds
    )
    assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
    assert result.rounds == rounds
    # 每轮都重规划过:6 轮 = 6 次 PLAN 模型请求(每个计划请求都不消耗脚本步)
    assert _llm_states(Path(result.run_dir)).count("PLAN") == rounds


def _measured_supersteps(run_dir: Path, task_id: str) -> int:
    """从检查点库读这次执行真的烧掉多少个 superstep(LangGraph metadata.step)。"""
    from app.graph.checkpoint import make_sqlite_checkpointer

    reader = make_sqlite_checkpointer(run_dir / "checkpoints.sqlite")
    assert reader is not None, "实测依赖检查点在场"
    try:
        tup = reader.get_tuple({"configurable": {"thread_id": task_id}})
        assert tup is not None
        return int((tup.metadata or {}).get("step", 0) or 0)
    finally:
        reader.conn.close()


def test_recursion_limit_formula_matches_measured_superstep_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`recursion_limit = 5N+8` 的斜率与余量以**实测**为准,不靠注释里的推算。

    满轮数重试路径每轮的 superstep 成本实测为 5(N=2/3/6 → 13/18/33,三点共线),
    固定段实测 3 步,所以余量恒等于 5 个 superstep、与轮数无关:把 max_rounds 抬到 20
    也不会像旧公式 4N+8 那样先撞上 LangGraph 的递归上限(那会把结构化的 BUDGET_EXCEEDED
    变成 NEEDS_REVIEW)。上一用例只证了"5N+8 在 N=6 够用",那条证明不了斜率对。

    这条同时是拓扑变动的绊线:往环里加节点 → 每轮成本变 → 本用例红,逼着同步改公式,
    而不是等某个长重试任务在生产里炸出来。pytest 全脚本化,零花费。
    """
    _scripted_pytest(monkeypatch, verify_green=False)
    bug = load_bug("BUG-001", BUG_ROOT)
    measured: dict[int, int] = {}
    for rounds in (2, 3, 6):
        script = _LOCALIZE + _propose(_NOOP_BLOCK) * rounds
        result = run_task_graph(
            bug, FakeLLM(list(script)), runs_root=tmp_path / f"r{rounds}", max_rounds=rounds
        )
        assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
        measured[rounds] = _measured_supersteps(Path(result.run_dir), result.task_id)

    slope = measured[3] - measured[2]
    assert measured[6] - measured[3] == 3 * slope, f"每轮成本不线性,公式无从谈起:{measured}"
    assert slope == 5, f"每轮 superstep 成本不再是 5,`5N+8` 的斜率要同步:{measured}"
    intercept = measured[2] - 2 * slope
    assert intercept == 3, f"固定段成本变了:{measured}"
    for rounds, steps in measured.items():
        assert 5 * rounds + 8 > steps, (rounds, steps)
    # 余量与 N 无关:这就是"轮数上限抬高也不会失守"的全部内容
    assert slope * 20 + intercept < 5 * 20 + 8
