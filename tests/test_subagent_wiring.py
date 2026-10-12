"""G13:轻量角色委派——接线与端到端测试(SPEC §G13)。

覆盖:
- e2e:主图 LOCALIZE 阶段调 delegate(researcher),子任务跑独立会话并返回;
  result.subtasks 落一条 researcher,子报告目录有轨迹;
- delegation 关闭:delegate 不进工具白名单(调用被"not allowed"挡下),无子任务记录;
- one_shot 恒不建 manager,工具表逐字节;
- 委派开启时 Best-of-N 候选经统一 child runner:全部落选也把子任务池消耗入账
  (children 2 条 repair_candidate,settled>0,reserved 归 0);
- CLI/API 两入口共用 app.agent.subtasks 的同一组模型工厂。
"""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

BUG001 = Path("bugs/BUG-001")
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"

FINISH_OK: dict[str, object] = {
    "tool": "finish",
    "args": {"success": True, "summary": "done"},
}


def _fix_block() -> str:
    steps = json.loads((BUG001 / "replay" / "script.json").read_text(encoding="utf-8"))
    return next(s["args"]["patch_text"] for s in steps if s.get("tool") == "apply_patch")


def _subagent_settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "delegation_enabled": True,
        "max_subagent_concurrency": 2,
        "max_delegations": 3,
        "subtask_budget_share": 0.25,
        "subtask_max_turns": 10,
        "subtask_model": "fake",
    }
    base.update(kw)
    return Settings().model_copy(update=base)


def _make_subagent_bug(dest_root: Path) -> Path:
    """复制 BUG-001,主脚本在 LOCALIZE 调 delegate(researcher),配 frozen 子任务脚本。"""
    bug_dir = dest_root / "BUG-SUB"
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BUG001, bug_dir)
    manifest = (
        (bug_dir / "manifest.yaml")
        .read_text(encoding="utf-8")
        .replace("id: BUG-001", "id: BUG-SUB", 1)
    )
    (bug_dir / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    localize = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {
            "tool": "delegate",
            "args": {"role": "researcher", "task": "确认根因", "evidence_ids": ["ev1"]},
        },
        {"tool": "finish", "args": {"success": True, "summary": "根因:空输入未处理"}},
    ]
    propose = [
        {"tool": "apply_patch", "args": {"patch_text": _fix_block()}},
        {"tool": "finish", "args": {"success": True, "summary": "补上空输入处理"}},
    ]
    (bug_dir / "replay" / "graph-script.json").write_text(
        json.dumps(localize + propose, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    (bug_dir / "replay" / "subtasks.json").write_text(
        json.dumps(
            {
                "researcher": [
                    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
                    {"tool": "finish", "args": {"success": True, "summary": "子任务确认根因"}},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return bug_dir


@pytest.fixture(scope="module")
def sub_bug_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sub-bugs")


# ---------- 端到端:delegate 走真图引擎 ----------


def test_end_to_end_delegate_resolves(sub_bug_root: Path, tmp_path: Path) -> None:
    from app.agent.subtasks import subtask_model_factory_for
    from app.evals.bugset import load_bug, load_replay_script
    from app.graph.runner import run_task_graph

    bug_dir = _make_subagent_bug(sub_bug_root)
    bug = load_bug(bug_dir, sub_bug_root)
    main_script = load_replay_script(bug, kind="graph")
    settings = _subagent_settings()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.graph.nodes.get_settings", lambda: settings)
        result = run_task_graph(
            bug,
            FakeLLM(main_script),
            runs_root=tmp_path / "runs",
            subtask_model_factory=subtask_model_factory_for(bug, "fake", settings),
        )
    assert result.verdict == "resolved", (result.status, result.error, result.gate_violations)
    assert len(result.subtasks) == 1
    sub = result.subtasks[0]
    assert sub["role"] == "researcher"
    assert sub["termination"] == "finished" and sub["ok"]
    assert sub["subtask_id"] == "sub1"
    sub_traj = Path(result.run_dir) / "reports" / "sub1" / "trajectory.jsonl"
    assert sub_traj.is_file()
    assert json.loads(sub_traj.read_text(encoding="utf-8").splitlines()[-1])["tool"] == "finish"


def test_end_to_end_delegation_off_blocks_delegate(sub_bug_root: Path, tmp_path: Path) -> None:
    """delegation 关闭:delegate 不进白名单 → 调用被挡,无子任务记录,修复仍收敛。"""
    from app.agent.subtasks import subtask_model_factory_for
    from app.evals.bugset import load_bug, load_replay_script
    from app.graph.runner import run_task_graph

    bug_dir = _make_subagent_bug(sub_bug_root / "off")
    bug = load_bug(bug_dir, sub_bug_root / "off")
    main_script = load_replay_script(bug, kind="graph")
    settings = _subagent_settings(delegation_enabled=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.graph.nodes.get_settings", lambda: settings)
        result = run_task_graph(
            bug,
            FakeLLM(main_script),
            runs_root=tmp_path / "runs",
            subtask_model_factory=subtask_model_factory_for(bug, "fake", settings),
        )
    assert result.verdict == "resolved", (result.status, result.error, result.gate_violations)
    assert result.subtasks == []
    assert not (Path(result.run_dir) / "reports" / "sub1").exists()
    events = [
        json.loads(line)
        for line in Path(result.run_dir, "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    blocked = [e for e in events if e["tool"] == "delegate"]
    assert blocked and "not allowed in phase" in blocked[0]["error"]


# ---------- 节点装配面 ----------


def _model() -> SimpleNamespace:
    return SimpleNamespace(provider="fake-replay", complete=lambda *a, **k: None)


def test_one_shot_never_builds_manager_or_delegate_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.evals.experiment_policy import one_shot_policy
    from app.graph.nodes import READ_TOOLS, TaskNodes

    monkeypatch.setattr(
        "app.graph.nodes.get_settings", lambda: SimpleNamespace(delegation_enabled=True)
    )
    bug = SimpleNamespace(
        id="BUG-X",
        allowed_paths=None,
        test_sets={"failed": [FAILED_ID], "regression": []},
        failed_tests=[FAILED_ID],
        regression_tests=[],
        repo_dir=Path("."),
        max_rounds=5,
    )
    nodes = TaskNodes(
        bug=bug,
        model=_model(),  # type: ignore[arg-type]
        workspace=Path("."),
        tracker=Tracker(tmp_path / "t.jsonl", task_id="T-X"),
        report_dir=tmp_path / "reports",
        max_rounds=5,
        max_turns=10,
        policy=one_shot_policy(),
    )
    assert nodes.subtask_manager is None
    assert nodes._with_delegation(READ_TOOLS) == READ_TOOLS  # 逐字节:不追加 delegate


def test_delegation_enabled_appends_delegate_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.graph.nodes import READ_TOOLS, TaskNodes

    monkeypatch.setattr(
        "app.graph.nodes.get_settings", lambda: SimpleNamespace(delegation_enabled=True)
    )
    bug = SimpleNamespace(
        id="BUG-X",
        allowed_paths=None,
        test_sets={"failed": [FAILED_ID], "regression": []},
        failed_tests=[FAILED_ID],
        regression_tests=[],
        repo_dir=Path("."),
        max_rounds=5,
    )
    nodes = TaskNodes(
        bug=bug,
        model=_model(),  # type: ignore[arg-type]
        workspace=Path("."),
        tracker=Tracker(tmp_path / "t.jsonl", task_id="T-X"),
        report_dir=tmp_path / "reports",
        max_rounds=5,
        max_turns=10,
    )
    assert nodes.subtask_manager is not None
    assert nodes._with_delegation(READ_TOOLS) == [*READ_TOOLS, "delegate"]


# ---------- Best-of-N 候选经统一 child runner:落选也入账 ----------


def _state_at_rollback() -> dict[str, object]:
    return {
        "bug_id": "BUG-001",
        "issue_text": "x",
        "round_no": 2,
        "repeat_streak": 2,
        "tokens_used": 0,
        "turns": 0,
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "findings": "",
        "feedback": "",
    }


def test_all_candidates_losing_charge_subtask_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """候选无补丁、双双落选:仍各消耗一个子任务名额并结算真实用量。"""
    from app.evals.bugset import load_bug
    from app.gitops.testing import materialize_repo
    from app.graph.nodes import TaskNodes

    bug = load_bug(BUG001, BUG001.parent)
    template = tmp_path / "template"
    shutil.copytree(BUG001 / "repo", template)
    (template / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8", newline="\n")
    ws = tmp_path / "ws"
    sha = materialize_repo(template, ws, extra_commit=False)
    settings = _subagent_settings()
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: settings)
    ctx = ToolContext(
        task_id="T-SUB-BR",
        workspace=ws,
        baseline_commit=sha,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-SUB-BR"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": []},
        allowed_paths=["src/**"],
    )
    nodes = TaskNodes(
        bug=bug,
        model=_model(),  # type: ignore[arg-type]
        workspace=ws,
        tracker=ctx.tracker,
        report_dir=tmp_path / "reports",
        max_rounds=5,
        max_turns=10,
    )
    nodes.ctx = ctx
    nodes.branch_model_factory = lambda i: FakeLLM(
        [
            {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
            FINISH_OK,  # type: ignore[arg-type]
        ]
    )
    assert nodes.subtask_manager is not None
    manager = nodes.subtask_manager
    update = nodes._run_fallback_branches(_state_at_rollback())  # type: ignore[arg-type]

    assert update["status"] == "PROPOSE_PATCH"  # 全落选 → 单线下一轮
    assert len(manager.children) == 2
    assert all(c.role == "repair_candidate" for c in manager.children)
    assert all(c.termination == "finished" for c in manager.children)
    assert manager.ledger.subtask_settled_tokens > 0  # 落选候选的真实用量已结算
    assert manager.ledger.subtask_reserved_tokens == 0  # 预留全部回收
    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert not [e for e in events if e["tool"] == "adaptive_branch_no_slot"]


# ---------- CLI/API 共用同一组工厂 ----------


def test_cli_and_api_share_subtask_factories() -> None:
    from app.api import service
    from app.evals import run_single

    cli_src = inspect.getsource(run_single)
    api_src = inspect.getsource(service)
    assert (
        "from app.agent.subtasks import branch_model_factory_for, subtask_model_factory_for"
        in api_src
    )
    assert "subtask_model_factory_for" in cli_src and "branch_model_factory_for" in cli_src
