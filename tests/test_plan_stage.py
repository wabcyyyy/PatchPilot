"""M5 PLAN 阶段用例:范式必须是 LOCALIZE → PLAN → ACT(propose/apply)→ VERIFY。

钉住三件事(节点级不变量与额度口径的对照用例在 tests/test_plan_invariants.py):
1. 计划是**必经节点**(轨迹里 PLAN 落在 LOCALIZE 之后、PROPOSE 之前),关闭时零请求零事件;
2. **脚本兼容契约**(本卡最大的脚枪):计划请求不得消耗 FakeLLM 的脚本步。既有 graph 用例
   都写成 `_localize_script() + _propose_script(...)`,计划一旦弹步就整体错位,而"顺手改
   脚本"会把错位掩盖成通过——下面 on/off 两臂的 `consumed` 对照就是这条契约的钉子;
3. 失败轮**重规划**:apply 重试边与 rollback 边都回到 plan,第二次计划请求带上一版计划、
   失败反馈与 M4 的回滚形状;计划段自己额度耗尽只是降级,不是终点。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.pytest_adapter import FailedCase, PytestReport
from app.config import get_settings
from app.errors import BudgetError
from app.evals.bugset import load_bug
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.prompts import PLAN_MARKER
from tests.conftest import block

BUG_ROOT = Path("bugs")
_LOCALIZE = [
    {"tool": "search_code", "args": {"keyword": "parse_date"}},
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "finish", "args": {"success": True, "summary": "根因:parse_date 未处理空白字符串"}},
]
# 合法但什么都没修好的补丁(制造"verify 失败 → 回滚 → 重规划"),以及改测试文件的补丁
# (制造"门禁拒绝 → PATCH_REJECTED 重试")
_NOOP_BLOCK = block(
    "--- a/src/dateparse.py\n+++ b/src/dateparse.py\n@@ -1,3 +1,4 @@\n"
    ' """日期解析工具。"""\n'
    " \n"
    "+# 方向未定,先记一笔\n"
    " from datetime import datetime\n"
)
_BAD_BLOCK = block(
    "diff --git a/tests/test_dateparse.py b/tests/test_dateparse.py\n"
    "--- a/tests/test_dateparse.py\n+++ b/tests/test_dateparse.py\n@@ -1,3 +1,4 @@\n"
    " import pytest\n"
    "+\n"
    " from src.dateparse import parse_date\n"
)


def _fix_block() -> str:
    """金补丁(块协议)取自题目自带的回放脚本,不在这里重复维护一份。"""
    steps = json.loads((BUG_ROOT / "BUG-001" / "replay" / "graph-script.json").read_text("utf-8"))
    return next(s["args"]["patch_text"] for s in steps if s.get("tool") == "apply_patch")


def _propose(patch_text: str, *, with_run_tests: bool = False) -> list[dict[str, Any]]:
    """一轮补丁阶段的脚本;run_tests 两步只在"真跑 pytest"的用例里出现(否则白等一轮)。"""
    steps: list[dict[str, Any]] = [{"tool": "apply_patch", "args": {"patch_text": patch_text}}]
    if with_run_tests:
        steps += [
            {"tool": "run_tests", "args": {"test_set": "failed"}},
            {"tool": "run_tests", "args": {"test_set": "regression"}},
        ]
    steps.append({"tool": "finish", "args": {"success": True, "summary": "补丁已应用"}})
    return steps


class _Recording(FakeLLM):
    """记录每次请求的全部文本(用来数计划请求),并暴露已消耗的脚本步数。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__(script)
        self.requests: list[str] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.requests.append(" ".join(str(m.get("content", "")) for m in messages))
        return super().complete(messages, tools)

    @property
    def plan_requests(self) -> list[str]:
        return [r for r in self.requests if PLAN_MARKER in r]


def _events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def _llm_states(run_dir: Path) -> list[str]:
    """轨迹里模型请求的阶段序列(= 节点访问顺序的可读投影)。"""
    return [str(e["state"]) for e in _events(run_dir) if e["tool"] == "llm"]


def _scripted_pytest(monkeypatch: pytest.MonkeyPatch, *, verify_green: bool) -> list[str]:
    """把 nodes.run_pytest 换成零成本脚本化报告,同时记下 junit 名(= 执行次数与次序)。

    基线的 failed 集必须真有失败、回归集必须绿,否则任务直接 INVALID_TASK 终止;
    verify 的结果由 verify_green 决定,而**双跑是否触发仍由 nodes.verify 的既有判据决定**——
    本桩不碰判定,所以它同时是"verify 双跑次数没被计划阶段移动"的度量工具。
    """
    import app.graph.nodes as nodes

    calls: list[str] = []
    red = PytestReport(
        exit_code=1,
        failed=1,
        collected=1,
        failed_cases=[
            FailedCase(
                test_name="test_empty_string_returns_none",
                test_id="tests/test_dateparse.py::test_empty_string_returns_none",
                kind="failure",
                message_first_line="AssertionError",
                signature="AssertionError: assert None",
            )
        ],
    )
    green = PytestReport(exit_code=0, passed=2, collected=2)

    def scripted(python_exe, workspace, test_ids, junit_path, *a, **kw):  # type: ignore[no-untyped-def]
        name = Path(junit_path).name
        calls.append(name)
        if name.startswith("baseline-failed"):
            return red, ""
        if name.startswith("baseline-regression"):
            return green, ""
        return (green if verify_green else red), ""

    monkeypatch.setattr(nodes, "run_pytest", scripted)
    return calls


def _initial(bug: Any, max_rounds: int) -> dict[str, Any]:
    """runner 的初始 state(runner 自己拼的那份;直连图的用例照抄一份)。"""
    return {
        "bug_id": bug.id,
        "issue_text": bug.issue_text,
        "failed_tests": bug.failed_tests,
        "regression_tests": bug.regression_tests,
        "allowed_paths": bug.allowed_paths,
        "max_rounds": max_rounds,
        "status": "CREATED",
        "round_no": 1,
        "turns": 0,
        "tokens_used": 0,
    }


# ---------- 图形状 + 脚本兼容契约 ----------


def test_plan_sits_between_localize_and_propose_and_eats_no_script_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旗舰用例:PLAN 在 LOCALIZE 与 PROPOSE 之间,且两臂消耗的脚本步数逐字相同。

    这条用真 pytest 全程跑(不用桩):它同时是"计划阶段进完整闭环后仍能 resolved"的证据。
    """
    bug = load_bug("BUG-001", BUG_ROOT)
    script = _LOCALIZE + _propose(_fix_block(), with_run_tests=True)
    on = _Recording(list(script))
    on_result = run_task_graph(bug, on, runs_root=tmp_path / "runs")
    assert on_result.status == "FINISHED" and on_result.verdict == "resolved"

    states = _llm_states(Path(on_result.run_dir))
    assert max(i for i, s in enumerate(states) if s == "LOCALIZE") < states.index("PLAN")
    assert states.index("PLAN") < states.index("PROPOSE_PATCH")
    assert len(on.plan_requests) == 1 and "### 定位阶段结论" in on.plan_requests[0]

    # 关闭臂:零计划请求、零 PLAN 事件,结论与开启臂一致
    real = get_settings()
    monkeypatch.setattr(
        "app.graph.nodes.get_settings",
        lambda: real.model_copy(update={"plan_stage_enabled": False}),
    )
    off = _Recording(list(script))
    off_result = run_task_graph(bug, off, runs_root=tmp_path / "runs-off")
    assert off_result.status == "FINISHED" and off_result.verdict == "resolved"
    assert not off.plan_requests and "PLAN" not in _llm_states(Path(off_result.run_dir))
    # 兼容性契约:两臂消耗的脚本步数相同,且都把脚本恰好走完——计划阶段一步都没弹
    assert on.consumed == off.consumed == len(script)


def test_plan_budget_share_defaults_and_validator() -> None:
    """默认开启;份额口径与 localize_budget_share 同((0,1] 之外一律拒)。"""
    from app.config import Settings

    settings = Settings()
    assert settings.plan_stage_enabled is True and settings.plan_budget_share == 0.15
    assert Settings(plan_budget_share=1.0).plan_budget_share == 1.0
    with pytest.raises(ValueError):
        Settings(plan_budget_share=0.0)
    with pytest.raises(ValueError):
        Settings(plan_budget_share=1.5)


# ---------- 失败轮重规划 ----------


def test_failed_round_replans_with_previous_plan_and_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify 失败 → 回滚 → 重试边先进 PLAN;第二次计划请求带上一版计划与失败反馈。"""
    _scripted_pytest(monkeypatch, verify_green=False)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = _Recording(_LOCALIZE + _propose(_NOOP_BLOCK) * 2)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=2)
    assert result.status == "BUDGET_EXCEEDED"  # 轮数耗尽的既有终态,未因重规划而移动

    plans = model.plan_requests
    assert len(plans) == 2, _llm_states(Path(result.run_dir))
    assert "### 上一版计划" not in plans[0] and "上一轮补丁应用后仍有失败" not in plans[0]
    assert "### 上一版计划" in plans[1] and "上一轮补丁应用后仍有失败" in plans[1]
    assert "上一轮补丁已回滚" in plans[1]  # M4 的 discarded-patch 形状跟着进重规划
    # 第二轮的 propose 必须排在第二次 PLAN 之后:重规划不是并行装饰
    states = _llm_states(Path(result.run_dir))
    second_plan = len(states) - 1 - states[::-1].index("PLAN")
    assert states.count("PLAN") == 2 and second_plan > states.index("PROPOSE_PATCH")


def test_gate_rejected_round_replans_before_the_retry_propose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门禁拒绝(PATCH_REJECTED)的重试边同样重规划,而不是带着旧计划再赌一次。"""
    _scripted_pytest(monkeypatch, verify_green=True)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = _Recording(_LOCALIZE + _propose(_BAD_BLOCK) + _propose(_fix_block()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved" and result.rounds == 2
    assert len(model.plan_requests) == 2
    assert "上一轮补丁被门禁拒绝" in model.plan_requests[1]


# ---------- 降级路径(端到端:降级不等于判死) ----------


def test_degraded_plan_end_to_end_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划请求抛 BudgetError 时任务照常走到补丁阶段并 resolved。"""

    class _PlanBudget:
        provider = "fake-replay"

        def __init__(self, inner: FakeLLM) -> None:
            self.inner = inner

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            if any(PLAN_MARKER in str(m.get("content", "")) for m in messages):
                raise BudgetError("agent loop tokens 200 exceed budget 100", tokens_spent=200)
            return self.inner.complete(messages, tools)

    _scripted_pytest(monkeypatch, verify_green=True)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = _PlanBudget(FakeLLM(_LOCALIZE + _propose(_fix_block())))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status == "FINISHED" and result.verdict == "resolved"
    degraded = [e for e in _events(Path(result.run_dir)) if e["tool"] == "plan_degraded"]
    assert len(degraded) == 1 and degraded[0]["state"] == "PLAN"
