"""卡5a:自适应分支的触发信号与前置条件(本卡不接线执行路径,factory 恒 None → 恒不分支)。

覆盖三件事:
1. 两类触发信号各能单独点火(同断言连续 2 轮 / 补丁连续 2 次应用失败);
2. 每个"值不值得分支"的前置都能单独熄火(开关、已分支过、无候选模型、轮数、预算);
3. `patch_fail_streak` 的计数口径:只有真应用失败计数,门禁/协议拒绝不计数,成功清零。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gitops.snapshot import create_workspace
from app.graph.nodes import TaskNodes
from app.graph.state import TaskState
from app.llm.base import Model
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
