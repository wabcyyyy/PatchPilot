"""卡5:自适应分支——触发信号口径(5a)与候选并行择优合流(5b)。

覆盖三件事:
1. 两类触发信号各能单独点火(同断言连续 2 轮 / 补丁连续 2 次应用失败);
2. 每个"值不值得分支"的前置都能单独熄火(开关、已分支过、无候选模型、轮数、预算);
3. `patch_fail_streak` 的计数口径:只有真应用失败计数,门禁/协议拒绝不计数,成功清零。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.gitops.snapshot import create_workspace
from app.graph.nodes import VARIANT_HINTS, BranchCandidate, TaskNodes
from app.graph.state import TaskState
from app.llm.base import Model
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.patching import apply_patch
from app.tools.tracker import Tracker
from tests.conftest import block

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"


def _fake_settings(**kw: object) -> SimpleNamespace:
    """只暴露 _should_branch 用到的字段,测试不受本机 .env 影响。"""
    values: dict[str, object] = {
        "adaptive_branching_enabled": True,
        "token_budget": 200_000,
        "branching_min_token_reserve": 30_000,
        "branch_candidates": 2,
        "max_patch_files": 5,
        "test_timeout_seconds": 120,
        "task_timeout_seconds": 900,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def _model() -> Model:
    return SimpleNamespace(provider="fake-replay", complete=lambda *a, **k: None)


def _nodes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ctx: object | None = None,
    factory: object | None = (lambda i: _model()),
    max_rounds: int = 5,
    settings: SimpleNamespace | None = None,
) -> TaskNodes:
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: settings or _fake_settings())
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
        model=_model(),
        workspace=Path("."),
        tracker=Tracker(Path("unused.jsonl"), task_id="T-X"),
        report_dir=Path("unused"),
        max_rounds=max_rounds,
        max_turns=10,
    )
    nodes.ctx = ctx  # type: ignore[assignment]
    nodes.branch_model_factory = factory  # type: ignore[assignment]
    return nodes


def _state(**kw: object) -> TaskState:
    base: TaskState = {"round_no": 2, "repeat_streak": 0, "tokens_used": 0}  # type: ignore[typeddict-item]
    base.update(kw)  # type: ignore[arg-type]
    return base


# ---------- 触发信号 ----------


def test_repeat_streak_two_triggers_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=0))
    assert nodes._should_branch(_state(repeat_streak=2)) is True


def test_patch_fail_streak_two_triggers_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=2))
    assert nodes._should_branch(_state(repeat_streak=0)) is True


def test_single_streak_does_not_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=1))
    assert nodes._should_branch(_state(repeat_streak=1)) is False


def test_no_ctx_does_not_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(monkeypatch, ctx=None)
    assert nodes._should_branch(_state(repeat_streak=0)) is False


# ---------- 前置条件熄火 ----------


def test_factory_none_keeps_single_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡5a 阶段工厂恒不注入 → 行为与 V1 完全一致(本卡的核心安全声明)。"""
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=3), factory=None)
    assert nodes._should_branch(_state(repeat_streak=5)) is False


def test_switch_off_disables_branching(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(
        monkeypatch,
        ctx=SimpleNamespace(patch_fail_streak=3),
        settings=_fake_settings(adaptive_branching_enabled=False),
    )
    assert nodes._should_branch(_state(repeat_streak=3)) is False


def test_already_branched_never_branches_again(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=3))
    assert nodes._should_branch(_state(repeat_streak=3, branching_used=True)) is False


@pytest.mark.parametrize("round_no,max_rounds,expected", [(4, 5, False), (3, 5, True)])
def test_round_reserve_boundary(
    monkeypatch, round_no: int, max_rounds: int, expected: bool
) -> None:
    nodes = _nodes(monkeypatch, ctx=SimpleNamespace(patch_fail_streak=2), max_rounds=max_rounds)
    assert nodes._should_branch(_state(round_no=round_no, repeat_streak=2)) is expected


def test_token_reserve_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    tight = _nodes(
        monkeypatch,
        ctx=SimpleNamespace(patch_fail_streak=2),
        settings=_fake_settings(token_budget=50_000, branching_min_token_reserve=30_000),
    )
    # 已花 30k → 余量 20k < 30k 门槛:不分支
    assert tight._should_branch(_state(repeat_streak=2, tokens_used=30_000)) is False
    # 已花 10k → 余量 40k ≥ 门槛:分支
    assert tight._should_branch(_state(repeat_streak=2, tokens_used=10_000)) is True
    # token_budget<=0 → 不限制(None),预算前置条件不拦
    unlimited = _nodes(
        monkeypatch,
        ctx=SimpleNamespace(patch_fail_streak=2),
        settings=_fake_settings(token_budget=0),
    )
    assert unlimited._should_branch(_state(repeat_streak=2, tokens_used=999_999)) is True


# ---------- patch_fail_streak 计数口径(经工具层真实路径) ----------


@pytest.fixture()
def tool_ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    return ToolContext(
        task_id="T-BRANCH",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-BRANCH"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": []},
    )


GOOD_BLOCK = block(
    "--- a/src/dateparse.py\n+++ b/src/dateparse.py\n@@ -17,6 +17,8 @@\n"
    '     """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""\n'
    "     if value is None:\n         return None\n"
    "+    if not value.strip():\n+        return None\n"
    "     # BUG: 空字符串/空白字符串未处理,strptime 抛 ValueError 直接冒泡\n"
)

# 锚点合法、git 能应用,但留下语法错误 → 走 patcher 的"应用后还原"路径(applied=False)
SYNTAX_BAD_BLOCK = (
    "*** Begin Patch\n*** Update File: src/dateparse.py\n"
    '     """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""\n'
    "     if value is None:\n         return None\n"
    "+    return [[[  # 语法错误\n*** End Patch\n"
)

GATE_REJECTED_BLOCK = (
    "*** Begin Patch\n*** Add File: tests/test_sneaky.py\n+def test_x():\n+    assert True\n"
    "*** End Patch\n"
)

PROTOCOL_REJECTED = "diff --git a/src/dateparse.py b/src/dateparse.py\n--- a/src/dateparse.py\n+++ b/src/dateparse.py\n@@ -1 +1 @@\n-x\n+y\n"


def test_streak_counts_only_real_apply_failures(tool_ctx: ToolContext) -> None:
    assert tool_ctx.patch_fail_streak == 0
    # ①协议拒绝:不计
    assert not apply_patch(tool_ctx, PROTOCOL_REJECTED).ok
    assert tool_ctx.patch_fail_streak == 0
    # ②门禁拒绝:不计(可修正的单线反馈)
    assert not apply_patch(tool_ctx, GATE_REJECTED_BLOCK).ok
    assert tool_ctx.patch_fail_streak == 0
    # ③真应用失败(语法预检还原):计 1
    bad = apply_patch(tool_ctx, SYNTAX_BAD_BLOCK)
    assert not bad.ok and "python_syntax_error" in bad.error
    assert tool_ctx.patch_fail_streak == 1
    # ④再来一次:计 2 → 满足分支信号②
    assert not apply_patch(tool_ctx, SYNTAX_BAD_BLOCK).ok
    assert tool_ctx.patch_fail_streak == 2
    # ⑤成功应用:清零
    assert apply_patch(tool_ctx, GOOD_BLOCK).ok
    assert tool_ctx.patch_fail_streak == 0


def test_streak_is_not_serialized_into_replay_scripts(tool_ctx: ToolContext) -> None:
    """信号是运行期上下文,不进回放语料:语料里的 apply_patch step 只有 patch_text。"""
    path = Path("bugs/BUG-001/replay/script.json")
    steps = json.loads(path.read_text(encoding="utf-8"))
    for step in steps:
        if step.get("tool") == "apply_patch":
            assert set(step["args"]) == {"patch_text"}


# ---------- 卡5b:择优、合流与门禁复核 ----------

BUG001 = Path("bugs/BUG-001")

# 合法但不修任何东西的补丁(基线失败签名保持完全一致 → 连两轮 → 触发分支)
NOOP_BLOCK = block(
    "--- a/src/dateparse.py\n+++ b/src/dateparse.py\n@@ -1,3 +1,4 @@\n"
    ' """日期解析工具。"""\n'
    " \n"
    "+# 方向未定,先记一笔\n"
    " from datetime import datetime\n"
)

# 候选"作弊":改测试文件让失败用例消失——候选自验会绿,但合流后必须被图级门禁拒
CHEAT_DIFF = (
    "--- a/tests/test_dateparse.py\n+++ b/tests/test_dateparse.py\n@@ -21,4 +21,4 @@\n"
    "\n"
    " def test_empty_string_returns_none():\n"
    '-    assert parse_date("") is None\n'
    "+    assert True\n"
    " \n"
)


def _fix_block() -> str:
    steps = json.loads((BUG001 / "replay" / "script.json").read_text(encoding="utf-8"))
    return next(s["args"]["patch_text"] for s in steps if s.get("tool") == "apply_patch")


def _candidate(
    index: int, *, failed_ok=True, regression_ok=True, remaining=0, diff=""
) -> SimpleNamespace:
    return BranchCandidate(
        index=index,
        workspace=Path(f"cand{index}"),
        diff_text=diff,
        failed_ok=failed_ok,
        regression_ok=regression_ok,
        failed_remaining=remaining,
        failed_cases=[] if failed_ok else [{"name": "test_x", "signature": "failure: same"}],
        turns=3,
        tokens_used=1_000 * (index + 1),
        tokens_prompt=600,
        tokens_completion=400,
    )


def _real_nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TaskNodes:
    """真题目 + 真工作区(不跑 LLM/pytest,候选由测试直接喂)。

    模板里加 `.gitattributes`(`* text eol=lf`):本机 git 默认 core.autocrlf=true,
    checkout 会把工作树变成 CRLF,测试手写的 LF unified diff 就"patch does not apply"
    (块协议不受影响——编译器按文件真实行尾生成 diff,故卡1 的语料用例一直是绿的)。
    """
    from app.gitops.testing import materialize_repo

    template = tmp_path / "template"
    shutil.copytree(BUG001 / "repo", template)
    (template / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8", newline="\n")
    ws = tmp_path / "ws"
    sha = materialize_repo(template, ws, extra_commit=False)
    bug = SimpleNamespace(
        id="BUG-001",
        root=BUG001,
        repo_dir=BUG001 / "repo",
        allowed_paths=["src/**"],
        test_sets={"failed": [], "regression": []},
        failed_tests=[FAILED_ID],
        regression_tests=[],
        issue_text="x",
        max_rounds=5,
    )
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: _fake_settings())
    ctx = ToolContext(
        task_id="T-BR",
        workspace=ws,
        baseline_commit=sha,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-BR"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": []},
        allowed_paths=["src/**"],
    )
    nodes = TaskNodes(
        bug=bug,
        model=_model(),
        workspace=ws,
        tracker=ctx.tracker,
        report_dir=tmp_path / "reports",
        max_rounds=5,
        max_turns=10,
    )
    nodes.ctx = ctx
    nodes.branch_model_factory = lambda i: _model()
    return nodes


def _state_at_rollback() -> dict[str, object]:
    """rollback 现场的 state(节点用 state[...] 取键,必须是 dict 不是命名空间)。"""
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


def test_select_candidate_prefers_fully_green_then_least_remaining() -> None:
    from app.graph.nodes import TaskNodes as T

    losers = [
        _candidate(0, failed_ok=False, remaining=2),
        _candidate(1, failed_ok=True, regression_ok=False, remaining=1),
        _candidate(2, failed_ok=True, regression_ok=True, remaining=0),
    ]
    assert T._select_candidate(losers).index == 2
    # 全败时取"剩余失败最少"的那个,而不是第一个
    all_bad = [
        _candidate(0, failed_ok=False, remaining=3),
        _candidate(1, failed_ok=False, remaining=1),
    ]
    assert T._select_candidate(all_bad).index == 1


def test_branch_winner_patch_is_merged_into_main_workspace(monkeypatch, tmp_path) -> None:
    nodes = _real_nodes(monkeypatch, tmp_path)
    # 合流阶段用的是候选工作区的 unified diff(working_tree_diff 产物),不是块文本:
    # 块只存在于模型↔apply_patch 工具边界,候选工作区取回的已经是内部事实源
    winner_diff = CHEAT_DIFF
    monkeypatch.setattr(
        nodes,
        "_run_candidate",
        lambda state, index, reserve: _candidate(
            index,
            failed_ok=index == 1,
            regression_ok=index == 1,
            remaining=0 if index == 1 else 2,
            diff=winner_diff if index == 1 else "",
        ),
    )
    update = nodes._run_fallback_branches(_state_at_rollback())  # type: ignore[arg-type]

    assert update["status"] == "APPLY_PATCH"
    assert update["branching_used"] is True and update["branch_selected"] == 1
    assert update["round_no"] == 3
    # 候选成本记回任务账(双倍开销必须可见,否则预算门禁被绕)
    assert update["tokens_used"] == 1_000 + 2_000
    assert update["turns"] == 6
    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    tools = [e["tool"] for e in events]
    assert "adaptive_branch_trigger" in tools and "adaptive_branch_selected" in tools
    assert (tmp_path / "ws" / "tests" / "test_dateparse.py").read_text(encoding="utf-8").count(
        "assert True"
    ) == 1


def test_merged_cheat_is_still_blocked_by_graph_gate(monkeypatch, tmp_path) -> None:
    """分支不给门禁开旁路:候选改测试让自己变绿,合流后 apply 节点照旧拒。"""
    nodes = _real_nodes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        nodes,
        "_run_candidate",
        lambda state, index, reserve: _candidate(
            index, failed_ok=True, regression_ok=True, remaining=0, diff=CHEAT_DIFF
        ),
    )
    update = nodes._run_fallback_branches(_state_at_rollback())  # type: ignore[arg-type]
    assert update["status"] == "APPLY_PATCH"

    state = dict(_state_at_rollback())
    state.update(update)
    state["round_no"] = 3
    result = nodes.apply(state)  # type: ignore[arg-type]
    assert result["status"] == "PATCH_REJECTED"
    assert any("[files]" in v for v in result["gate_violations"]), result["gate_violations"]


def test_all_candidates_without_patch_falls_back_to_single_line(monkeypatch, tmp_path) -> None:
    nodes = _real_nodes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        nodes,
        "_run_candidate",
        lambda state, index, reserve: _candidate(index, failed_ok=False, remaining=2, diff=""),
    )
    update = nodes._run_fallback_branches(_state_at_rollback())  # type: ignore[arg-type]
    assert update["status"] == "PROPOSE_PATCH"
    assert update["round_no"] == 3
    assert update["branching_used"] is True  # 至多一次:下一轮不再分支
    assert "自适应分支" in update["feedback"]
    # 主工作区没被动过
    from app.gitops.rollback import working_tree_is_clean

    assert working_tree_is_clean(nodes.workspace)


def test_route_rollback_three_way() -> None:
    t = TaskNodes(
        bug=SimpleNamespace(allowed_paths=None),
        model=_model(),
        workspace=Path("."),
        tracker=Tracker(Path("x.jsonl"), task_id="t"),
        report_dir=Path("."),
        max_rounds=5,
        max_turns=5,
    )
    assert t.route_rollback({"status": "BUDGET_EXCEEDED"}) == "end"
    assert t.route_rollback({"status": "APPLY_PATCH"}) == "apply"
    assert t.route_rollback({"status": "PROPOSE_PATCH"}) == "propose"


def test_variant_hints_are_distinct_directions() -> None:
    assert set(VARIANT_HINTS) == {0, 1}
    assert VARIANT_HINTS[0] != VARIANT_HINTS[1]
    assert "前置条件" in VARIANT_HINTS[0] and "边界输入" in VARIANT_HINTS[1]


# ---------- 端到端:真 graph 引擎 + 真 pytest + FakeLLM 候选 ----------


def _make_branch_bug(dest_root: Path) -> Path:
    bug_dir = dest_root / "BUG-BRANCH"
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BUG001, bug_dir)
    manifest = (
        (bug_dir / "manifest.yaml")
        .read_text(encoding="utf-8")
        .replace("id: BUG-001", "id: BUG-BRANCH", 1)
    )
    (bug_dir / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    localize = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "finish", "args": {"success": True, "summary": "根因:空输入未处理"}},
    ]
    # 两轮"合法但没修任何东西"的补丁:同一断言连两轮失败 → 触发分支
    wrong_round = [
        {"tool": "apply_patch", "args": {"patch_text": NOOP_BLOCK}},
        {"tool": "finish", "args": {"success": True, "summary": "以为修好了"}},
    ]
    (bug_dir / "replay" / "graph-script.json").write_text(
        json.dumps(localize + wrong_round + wrong_round, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    (bug_dir / "replay" / "graph-branches.json").write_text(
        json.dumps(
            {
                "0": [
                    {"tool": "apply_patch", "args": {"patch_text": _fix_block()}},
                    {"tool": "finish", "args": {"success": True, "summary": "根因收口"}},
                ],
                "1": [
                    {"tool": "apply_patch", "args": {"patch_text": NOOP_BLOCK}},
                    {"tool": "finish", "args": {"success": True, "summary": "仍没修好"}},
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
def branch_bug_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("branch-bugs")


def test_end_to_end_branch_resolves(branch_bug_root: Path, tmp_path: Path) -> None:
    from app.evals.bugset import load_bug, load_replay_script
    from app.evals.run_single import _branch_model_factory
    from app.graph.runner import run_task_graph

    bug_dir = _make_branch_bug(branch_bug_root)
    bug = load_bug(bug_dir, branch_bug_root)
    main_script = load_replay_script(bug, kind="graph")
    result = run_task_graph(
        bug,
        FakeLLM(main_script),
        runs_root=tmp_path / "runs",
        branch_model_factory=_branch_model_factory(bug, "fake", get_settings()),
    )
    assert result.verdict == "resolved", (result.status, result.error, result.gate_violations)
    assert result.rounds == 3, result.rounds  # 第 3 轮由分支合流,不是第三次 propose

    events = [
        json.loads(line)
        for line in Path(result.run_dir, "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    trigger = [e for e in events if e["tool"] == "adaptive_branch_trigger"]
    selected = [e for e in events if e["tool"] == "adaptive_branch_selected"]
    assert len(trigger) == 1 and len(selected) == 1  # 至多触发一次
    assert trigger[0]["input"]["reason"].startswith("repeat_streak=2")
    assert selected[0]["output_summary"]["winner"] == 0
    assert (Path(result.run_dir) / "reports" / "workspace-cand0").is_dir()


def test_end_to_end_switch_off_stays_single_line(branch_bug_root: Path, tmp_path: Path) -> None:
    """adaptive_branching_enabled=false → 功能级回退,行为与 V1 一致(不多花一次候选)。"""
    from app.config import Settings
    from app.evals.bugset import load_bug, load_replay_script
    from app.graph.runner import run_task_graph

    monkeypatch_settings = Settings().model_copy(update={"adaptive_branching_enabled": False})
    bug_dir = _make_branch_bug(branch_bug_root / "off")
    bug = load_bug(bug_dir, branch_bug_root / "off")
    main_script = load_replay_script(bug, kind="graph")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.graph.nodes.get_settings", lambda: monkeypatch_settings)
        result = run_task_graph(
            bug,
            FakeLLM(main_script),
            runs_root=tmp_path / "runs",
            branch_model_factory=lambda i: FakeLLM([{"tool": "finish", "args": {"success": True}}]),
        )
    assert result.verdict == "failed", result.verdict
    events = [
        json.loads(line)
        for line in Path(result.run_dir, "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not [e for e in events if e["tool"].startswith("adaptive_branch")]
    assert not (Path(result.run_dir) / "reports" / "workspace-cand0").exists()
