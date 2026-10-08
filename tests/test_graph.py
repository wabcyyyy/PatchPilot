"""M5 LangGraph 状态机测试:覆盖企划书 4.2 转移表的主要分支。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from app.errors import BudgetError
from app.evals.bugset import load_bug
from app.graph.builder import build_graph
from app.graph.nodes import READ_TOOLS, TaskNodes
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker
from tests.conftest import block

BUG_ROOT = Path("bugs")
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"


def _fix_diff() -> str:
    """BUG-001 的正确修复 diff(临时副本上生成)。"""
    tmp = Path(tempfile.mkdtemp(prefix="gfix-"))
    try:
        from app.gitops.differ import working_tree_diff
        from app.gitops.snapshot import create_workspace
        from app.gitops.testing import materialize_repo

        repo_src = tmp / "src"
        materialize_repo(BUG_ROOT / "BUG-001" / "repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "    if value is None:\n        return None\n",
                "    if value is None:\n        return None\n    if not value.strip():\n        return None\n",
            ),
            encoding="utf-8",
            newline="\n",
        )
        return working_tree_diff(tmp / "ws").diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _comment_diff() -> str:
    """不修复问题的合法补丁(只加注释),用于制造"verify 失败但门禁通过"的场景。"""
    tmp = Path(tempfile.mkdtemp(prefix="gcomment-"))
    try:
        from app.gitops.differ import working_tree_diff
        from app.gitops.snapshot import create_workspace
        from app.gitops.testing import materialize_repo

        repo_src = tmp / "src"
        materialize_repo(BUG_ROOT / "BUG-001" / "repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                '    raise ValueError(f"unrecognized date format: {value!r}")\n',
                '    raise ValueError(f"unrecognized date format: {value!r}")  # noqa: 见 issue\n',
            ),
            encoding="utf-8",
            newline="\n",
        )
        return working_tree_diff(tmp / "ws").diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _localize_script() -> list[dict]:
    return [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {
            "tool": "finish",
            "args": {"success": True, "summary": "根因:parse_date 未处理空白字符串"},
        },
    ]


def _propose_script(diff_text: str) -> list[dict]:
    return [
        {"tool": "apply_patch", "args": {"patch_text": block(diff_text)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "补丁已应用且测试通过"}},
    ]


def test_graph_resolves_bug001(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.engine == "graph"
    assert result.status == "FINISHED" and result.verdict == "resolved"
    assert result.rounds == 1
    assert result.changed_files == ["src/dateparse.py"]
    assert (Path(result.run_dir) / "trajectory.jsonl").exists()
    assert (Path(result.run_dir) / "checkpoints.sqlite").exists()


def test_graph_invalid_task(tmp_path: Path) -> None:
    bug = load_bug("BUG-003", BUG_ROOT)
    bug_dir = tmp_path / "BUG-003-FIXED"
    shutil.copytree(bug.root, bug_dir)
    (bug_dir / "repo" / "src" / "labels.py").write_text(
        'def join_labels(labels, sep=None):\n    separator = "," if sep is None else sep\n'
        '    result = ""\n    for index, label in enumerate(labels):\n'
        "        if index > 0:\n            result += separator\n        result += label\n    return result\n",
        encoding="utf-8",
        newline="\n",
    )
    fixed_bug = load_bug(bug_dir)
    model = FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "nothing"}}])
    result = run_task_graph(fixed_bug, model, runs_root=tmp_path / "runs")
    assert result.status == "INVALID_TASK"


def test_graph_patch_rejected_then_retry_resolves(tmp_path: Path) -> None:
    """第 1 轮补丁(改测试文件)被拒 → 回 PROPOSE(round=2)→ 正确补丁 → resolved。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    bad_patch = [
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
    model = FakeLLM(_localize_script() + bad_patch + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved"
    assert result.rounds == 2  # 第 1 轮被拒,第 2 轮修复
    assert result.changed_files == ["src/dateparse.py"]  # 回滚生效,坏补丁没有残留


def test_graph_budget_exhausted_after_failed_verify(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_comment_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=1)

    assert result.status == "BUDGET_EXCEEDED"
    assert result.verdict == "failed"
    assert result.verify_failed_ok is False
    # N-12 整改:末轮回滚前必须保全现场——diff.patch 是回滚前的工作区 diff,
    # 而不是 reset 后的空文件
    assert (Path(result.run_dir) / "diff.patch").read_text(encoding="utf-8").strip()


def test_graph_verify_failed_when_agent_gives_up(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    give_up = [{"tool": "finish", "args": {"success": False, "summary": "我修不了"}}]
    model = FakeLLM(_localize_script() + give_up)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "VERIFY_FAILED" and result.verdict == "failed"


def test_phase_tool_restriction(tmp_path: Path) -> None:
    """定位阶段禁用 apply_patch:工具调用被拒并记录轨迹。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    from app.gitops.testing import materialize_repo

    work = tmp_path / "ws"
    baseline = materialize_repo(bug.repo_dir, work, extra_commit=False)
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="T-PHASE")
    ctx = ToolContext(
        task_id="T-PHASE",
        workspace=work,
        baseline_commit=baseline,
        tracker=tracker,
        report_dir=tmp_path / "reports",
        test_sets=bug.test_sets,
    )
    model = FakeLLM(
        [
            *_localize_script()[:1],
            {"tool": "apply_patch", "args": {"patch_text": "junk"}},
            {"tool": "finish", "args": {"success": True, "summary": "x"}},
        ]
    )
    from app.graph.plain_loop import run_plain_loop

    run_plain_loop(ctx, model, "issue", allowed_tools=READ_TOOLS, state_label="LOCALIZE")
    events = [e for e in tracker.events if e.tool == "apply_patch"]
    assert events and "not allowed in phase" in (events[0].error or "")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    """带 SqliteSaver 全程执行任务无异常,且 state 每档都可序列化。

    M6 起检查点不再只是留档:恢复侧按 thread_id 读回它(见 app/graph/resume.py),
    所以"可序列化"从注释承诺升为可测属性;跨进程续跑的行为证明在 tests/test_resume.py。
    """
    from app.graph.checkpoint import make_sqlite_checkpointer
    from app.graph.state import TaskState

    bug = load_bug("BUG-001", BUG_ROOT)
    cp_db = tmp_path / "cp" / "checkpoints.sqlite"
    checkpointer = make_sqlite_checkpointer(cp_db)
    assert checkpointer is not None

    nodes = TaskNodes(
        bug=bug,
        model=FakeLLM(_localize_script() + _propose_script(_fix_diff())),
        workspace=tmp_path / "ws",
        tracker=Tracker(None, task_id="T-CP"),
        report_dir=tmp_path / "reports",
        max_rounds=bug.max_rounds,
        max_turns=20,
        started_monotonic=__import__("time").monotonic(),
    )
    graph = build_graph(nodes, checkpointer=checkpointer)
    thread = {"configurable": {"thread_id": "T-CP"}}
    final: TaskState = graph.invoke(
        {
            "bug_id": bug.id,
            "issue_text": bug.issue_text,
            "failed_tests": bug.failed_tests,
            "regression_tests": bug.regression_tests,
            "allowed_paths": bug.allowed_paths,
            "max_rounds": bug.max_rounds,
            "status": "CREATED",
            "round_no": 1,
            "turns": 0,
            "tokens_used": 0,
        },
        config=thread,
    )
    assert final["status"] == "FINISHED"
    # M5:state 新增的 plan 必须是普通 str——它要能被 SqliteSaver 原样序列化进上面这个检查点库
    assert isinstance(final.get("plan"), str) and final["plan"]
    # M6:恢复侧从同一个检查点读回 TaskState,新增的 baseline_commit 同样必须是普通 str
    # (reset_workspace 需完整 sha;轨迹里的 12 位前缀不足以复位工作区)
    assert isinstance(final.get("baseline_commit"), str)
    assert len(final["baseline_commit"]) == 40

    resumed: TaskState = graph.invoke(None, config=thread)
    assert resumed["status"] == "FINISHED" and resumed["outcome"] == "resolved"
    assert cp_db.exists() and cp_db.stat().st_size > 0


def test_graph_needs_review_when_localize_fails(tmp_path: Path) -> None:
    """定位阶段模型声明失败 → NEEDS_REVIEW 转人工,不产生补丁。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    give_up = [{"tool": "finish", "args": {"success": False, "summary": "找不到根因"}}]
    model = FakeLLM([*give_up, {"tool": "finish", "args": {"success": True, "summary": "unused"}}])
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "NEEDS_REVIEW" and result.verdict == "needs_review"
    assert result.verify_failed_ok is False


def test_graph_budget_exceeded_when_localize_blows_budget(tmp_path: Path) -> None:
    """定位阶段 token 预算耗尽 → BUDGET_EXCEEDED,不再被吞成 NEEDS_REVIEW。"""
    bug = load_bug("BUG-001", BUG_ROOT)

    class OverBudgetModel:
        provider = "fake-replay"

        def complete(self, messages, tools):
            raise BudgetError("agent loop tokens 999999 exceed budget 1000")

    result = run_task_graph(bug, OverBudgetModel(), runs_root=tmp_path / "runs")
    assert result.status == "BUDGET_EXCEEDED" and result.verdict == "failed"


def test_graph_cancelled_when_event_preset(tmp_path: Path) -> None:
    """取消事件预置:定位阶段 turn 边界抛 TaskCancelled → 收敛 CANCELLED,
    不被 localize/propose 的兜底吞成 NEEDS_REVIEW,报告正常落盘。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    event = threading.Event()
    event.set()

    class NeverModel:
        provider = "fake-replay"

        def complete(self, messages, tools):
            raise AssertionError("取消已置位,模型不应再被调用")

    result = run_task_graph(bug, NeverModel(), runs_root=tmp_path / "runs", cancel_event=event)
    assert result.status == "CANCELLED" and result.verdict == "cancelled"
    assert "cancelled at turn 1 boundary" in (result.error or "")

    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "CANCELLED"
    assert (Path(result.run_dir) / "trajectory.jsonl").exists()  # 现场保留


# ---------- 审计整改(P1-1):PROPOSE 阶段必须携带 bug 描述与定位结论 ----------


def test_propose_prompt_carries_issue_and_findings(tmp_path: Path) -> None:
    """PROPOSE 是全新会话:若提示词缺 issue/findings,真实模型在盲改。"""
    from app.llm.fake import FakeLLM

    bug = load_bug("BUG-001", BUG_ROOT)
    captured: dict = {}

    class ProbeModel(FakeLLM):
        def complete(self, messages, tools):
            captured["messages"] = messages
            return super().complete(messages, tools)

    model = ProbeModel(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.verdict == "resolved"

    user_texts = " ".join(
        str(m.get("content", "")) for m in captured["messages"] if m.get("role") == "user"
    )
    assert "日期解析" in user_texts  # issue_text 已带入 PROPOSE
    assert "根因" in user_texts  # 定位结论已带入 PROPOSE


def test_graph_budget_exceeded_in_propose_does_not_reach_verify(tmp_path: Path) -> None:
    """N-5 整改:propose 超预算必须终止——即使补丁已在工作区且真实有效,
    也不得继续 apply/verify 把资源门禁"顺路"绕过去、最终误判 FINISHED。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    inner = FakeLLM(
        [
            *_localize_script(),
            {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
            {"tool": "finish", "args": {"success": True, "summary": "补丁已应用"}},
        ]
    )
    calls = {"n": 0}

    class BlowBudgetInPropose:
        provider = "fake-replay"

        def complete(self, messages, tools):
            calls["n"] += 1
            if calls["n"] >= 5:  # 1-3 轮 LOCALIZE,4 是 apply_patch 所在 turn
                raise BudgetError("agent loop tokens 999999 exceed budget 1")
            return inner.complete(messages, tools)

    result = run_task_graph(bug, BlowBudgetInPropose(), runs_root=tmp_path / "runs")
    assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
    assert result.verdict == "failed"
    # verify 从未执行:判定字段保持默认 False,而不是被"顺路"的 verify 刷成 True
    assert result.verify_failed_ok is False and result.verify_regression_ok is False


def test_graph_last_round_gate_rejection_preserves_evidence(tmp_path: Path) -> None:
    """N-12 整改:末轮被终局门禁拒绝的补丁——终态 BUDGET_EXCEEDED(企划书 4.2)
    但 diff.patch 保留被拒补丁全文,取证现场不因回滚销毁。

    场景:两片补丁各自合法(3 文件 ≤ max_patch_files=5,工具层放行),
    合计 6 文件触发终局聚合 [scope] 拒绝——补丁已在工作区,回滚前必须保全。
    """

    def _padding_diff(start: int) -> str:
        parts = []
        for i in range(start, start + 3):
            name = f"src/pad{i}.py"
            parts.append(
                f"diff --git a/{name} b/{name}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{name}\n"
                "@@ -0,0 +1 @@\n"
                "+padding\n"
            )
        return "".join(parts)

    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(
        [
            *_localize_script(),
            {"tool": "apply_patch", "args": {"patch_text": block(_padding_diff(1))}},
            {"tool": "apply_patch", "args": {"patch_text": block(_padding_diff(4))}},
            {"tool": "finish", "args": {"success": True, "summary": "done"}},
        ]
    )
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", max_rounds=1)
    assert result.status == "BUDGET_EXCEEDED"
    assert any("[scope]" in v for v in result.gate_violations), result.gate_violations
    # 被拒补丁全文保留在 diff.patch(而非 reset 后的空 diff)
    assert (Path(result.run_dir) / "diff.patch").read_text(encoding="utf-8").strip()


# ---------- E3:verify 双跑一致性复核 ----------


def _count_node_pytest(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """给 nodes.run_pytest 包一层计数器(透传真实执行),返回记录 junit 文件名的列表。"""
    import app.graph.nodes as nodes

    calls: list[str] = []
    real_run = nodes.run_pytest

    def counting_run(python_exe, workspace, test_ids, junit_path, *args, **kwargs):
        calls.append(Path(junit_path).name)
        return real_run(python_exe, workspace, test_ids, junit_path, *args, **kwargs)

    monkeypatch.setattr(nodes, "run_pytest", counting_run)
    return calls


def test_verify_double_run_resolved_runs_pytest_six_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3:resolved 路径(基线 2 + verify 第一遍 2 + 复核 2)pytest 恰 6 次拉起,
    且任务 FINISHED、两次 junit 证据文件齐全。"""
    calls = _count_node_pytest(monkeypatch)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status == "FINISHED" and result.verdict == "resolved"
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
    run_dir = Path(result.run_dir)
    assert (run_dir / "reports" / "verify-failed-rerun.xml").exists()
    assert (run_dir / "reports" / "verify-regression-rerun.xml").exists()


def test_verify_double_run_mismatch_converges_needs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二遍与第一遍不一致(junit 伪造需同时伪造两次运行):NEEDS_REVIEW,
    error 带 verify_mismatch,轨迹记录两次运行摘要。"""
    import json

    import app.graph.nodes as nodes
    from app.adapters.pytest_adapter import PytestReport

    pass_report = PytestReport(exit_code=0, passed=2, collected=2)
    fail_report = PytestReport(exit_code=1, failed=1, collected=1)

    def scripted_run(python_exe, workspace, test_ids, junit_path, *args, **kwargs):
        name = Path(junit_path).name
        if name.startswith("baseline-failed"):
            return fail_report, ""  # 基线必须真有失败,否则 INVALID_TASK
        if name.startswith("baseline-regression"):
            return pass_report, ""
        if name.startswith("verify-failed-rerun"):
            return fail_report, ""  # 复核遍出现失败 → 与第一遍(全绿)不一致
        return pass_report, ""

    monkeypatch.setattr(nodes, "run_pytest", scripted_run)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status == "NEEDS_REVIEW" and result.verdict == "needs_review"
    assert "verify_mismatch" in (result.error or "")
    assert result.verify_failed_ok is False and result.verify_regression_ok is False
    events = [
        json.loads(line)
        for line in (Path(result.run_dir) / "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    double_events = [e for e in events if e.get("tool") == "verify_double_run"]
    assert double_events and double_events[0]["error"].startswith("verify_mismatch")
    assert double_events[0]["output_summary"]["first"]["failed_ok"] is True
    assert double_events[0]["output_summary"]["rerun"]["failed_ok"] is False


def test_verify_double_run_disabled_runs_pytest_four_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_double_run=False:回归为 4 次拉起,行为与引入 double-run 之前一致。"""
    from app.config import get_settings

    calls = _count_node_pytest(monkeypatch)
    real_settings = get_settings()
    monkeypatch.setattr(
        "app.graph.nodes.get_settings",
        lambda: real_settings.model_copy(update={"verify_double_run": False}),
    )
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status == "FINISHED" and result.verdict == "resolved"
    assert sorted(calls) == sorted(
        [
            "baseline-failed.xml",
            "baseline-regression.xml",
            "verify-failed.xml",
            "verify-regression.xml",
        ]
    )
    assert not (Path(result.run_dir) / "reports" / "verify-failed-rerun.xml").exists()


def test_graph_wrong_failed_test_id_never_resolves(tmp_path: Path) -> None:
    """P3-17/R3-Q5 空集陷阱·判定层集成用例:bug 带错测试 id → pytest rc=4
    (usage error),verify 每轮都不绿,终态不得为 FINISHED/resolved——
    「junit 无匹配」永远不等于「全部通过」。

    实测收敛(R3-Q5 草案预期 BUDGET_EXCEEDED,实际更早):第 1 轮 verify 不绿
    回滚后,FakeLLM 脚本耗尽 → 模型声明放弃且无补丁 → VERIFY_FAILED。
    钉死的是「带错 id 的 bug 永远不得 resolved」,具体失败终态是哪一种
    属引擎路由语义(给了补丁轮尽是 BUDGET_EXCEEDED,放弃是 VERIFY_FAILED)。
    """
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.failed_tests = ["tests/test_dateparse.py::test_does_not_exist"]  # 错 id
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.status in {"VERIFY_FAILED", "BUDGET_EXCEEDED"}
    assert result.verdict != "resolved"
    assert result.verify_failed_ok is False


# ---------- P1-c:重复错误检测与换思路提示 ----------


def _capture_feedback(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """捕获 nodes 层每次 build_feedback 调用(白盒:验证连续相同检测的注入点)。"""
    import app.graph.nodes as nodes_mod

    captured: list[dict] = []
    real = nodes_mod.build_feedback

    def spy(failed_cases: list[dict], extra_note: str = "", repeat_streak: int = 0) -> str:
        text = real(failed_cases, extra_note, repeat_streak)
        captured.append(
            {"cases": failed_cases, "extra": extra_note, "streak": repeat_streak, "text": text}
        )
        return text

    monkeypatch.setattr(nodes_mod, "build_feedback", spy)
    return captured


def test_feedback_streak_counts_and_resets() -> None:
    """连续相同才累加;集合变化重置;空白归零;比对与顺序无关。"""
    from app.graph.nodes import _feedback_streak

    assert _feedback_streak({}, ["b|2", "a|1"]) == (["a|1", "b|2"], 1)
    prev = {"last_feedback_signatures": ["a|1", "b|2"], "repeat_streak": 1}
    assert _feedback_streak(prev, ["b|2", "a|1"]) == (["a|1", "b|2"], 2)
    changed = {"last_feedback_signatures": ["a|1", "b|2"], "repeat_streak": 1}
    assert _feedback_streak(changed, ["a|1"]) == (["a|1"], 1)
    empty = {"last_feedback_signatures": ["a|1"], "repeat_streak": 2}
    assert _feedback_streak(empty, []) == ([], 0)


def test_build_feedback_repeat_hint_threshold() -> None:
    """提示只在连续 ≥2 轮时注入。"""
    from app.prompts import build_feedback

    cases = [{"name": "t", "signature": "failure: X"}]
    assert "换一种定位方向" not in build_feedback(cases, repeat_streak=1)
    hinted = build_feedback(cases, repeat_streak=2)
    assert "连续 2 轮" in hinted and "换一种定位方向" in hinted


def test_repeat_failure_streak_prompts_new_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify 连续两轮同一失败签名 → 第二轮反馈注入"换思路"提示。"""
    captured = _capture_feedback(monkeypatch)
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(
        _localize_script() + _propose_script(_comment_diff()) + _propose_script(_comment_diff())
    )
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    verify_feedback = [c for c in captured if c["cases"]]
    assert len(verify_feedback) == 2
    assert [c["streak"] for c in verify_feedback] == [1, 2]
    assert "连续 2 轮" in verify_feedback[1]["text"]
    assert result.verdict != "resolved"


def test_repeat_gate_rejection_streak_prompts_new_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门禁拒绝连续两轮相同 → 同样注入提示(与失败签名共用计数,互不误认)。"""
    captured = _capture_feedback(monkeypatch)
    bad_patch = [
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
    give_up = [{"tool": "finish", "args": {"success": False, "summary": "放弃"}}]
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + bad_patch + bad_patch + give_up)
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    gate_feedback = [c for c in captured if not c["cases"] and c["extra"]]
    assert [c["streak"] for c in gate_feedback[:2]] == [1, 2]
    assert "连续 2 轮" in gate_feedback[1]["text"]
    assert result.verdict != "resolved"


def test_verify_deadline_overrun_returns_budget_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复盘 P1-6:任务级 deadline 已过时,verify 入口即以 BUDGET_EXCEEDED 终态收尾,
    不再发起 pytest(双跑最多 4 次,不得越过 task_timeout_seconds)。"""
    import time
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.graph.nodes.get_settings",
        lambda: SimpleNamespace(task_timeout_seconds=900, verify_double_run=False),
    )

    def _no_pytest(*args: object, **kwargs: object) -> None:
        raise AssertionError("deadline 已到,verify 不得再发起 pytest")

    monkeypatch.setattr("app.graph.nodes.run_pytest", _no_pytest)

    nodes = TaskNodes(
        bug=SimpleNamespace(failed_tests=[], regression_tests=[]),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        tracker=Tracker(None, task_id="T-DL"),
        report_dir=tmp_path,
        max_rounds=3,
        max_turns=5,
        started_monotonic=time.monotonic() - 10_000,  # 早已超 900s
    )
    nodes.ctx = SimpleNamespace(python_exe="python", env=None)  # type: ignore[assignment]

    update = nodes.verify({"round_no": 1})  # type: ignore[arg-type]
    assert update["status"] == "BUDGET_EXCEEDED"
    assert "time budget" in update["error"]
    # 终态必须路由到终点,不得落进 finish/rollback 的标志位判断
    assert nodes.route_verify({"status": "BUDGET_EXCEEDED"}) == "end"  # type: ignore[arg-type]


def test_verify_deadline_hits_midway_keeps_partial_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复盘 P1-6:failed 集跑完、regression 之前撞 deadline——部分结果随终态带回。"""
    from types import SimpleNamespace

    clock = {"now": 1000.0}
    monkeypatch.setattr("app.graph.nodes.time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(
        "app.graph.nodes.get_settings",
        lambda: SimpleNamespace(task_timeout_seconds=900, verify_double_run=False),
    )
    partial = SimpleNamespace(all_passed=True, failed_cases=[], failed=0, errors=0)

    def _advance_after_first(*args: object, **kwargs: object) -> object:
        clock["now"] = 1001.0  # 第一次 pytest 之后时间越过 deadline(100+900)
        return (partial, None)

    monkeypatch.setattr("app.graph.nodes.run_pytest", _advance_after_first)

    nodes = TaskNodes(
        bug=SimpleNamespace(failed_tests=[], regression_tests=[]),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        tracker=Tracker(None, task_id="T-DL2"),
        report_dir=tmp_path,
        max_rounds=3,
        max_turns=5,
        started_monotonic=100.0,  # deadline = 100 + 900 = 1000
    )
    nodes.ctx = SimpleNamespace(python_exe="python", env=None)  # type: ignore[assignment]

    update = nodes.verify({"round_no": 1})  # type: ignore[arg-type]
    assert update["status"] == "BUDGET_EXCEEDED"
    assert update["verify_failed_ok"] is True


def test_checkpointer_init_failure_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复盘 R-4:checkpoint.py 的两条降级路径(依赖缺失/初始化失败)必须返回 None
    而不是抛出——任务照常执行,只是不可恢复。"""
    import sys

    from app.graph.checkpoint import make_sqlite_checkpointer

    # 初始化失败:父路径是普通文件,mkdir 必然抛 OSError 系
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert make_sqlite_checkpointer(blocker / "sub" / "cp.sqlite") is None

    # 依赖缺失:import 命中 sys.modules 的 None 占位 → ImportError
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", None)
    assert make_sqlite_checkpointer(tmp_path / "cp.sqlite") is None


def test_graph_crash_still_writes_diff_patch_and_report(tmp_path: Path) -> None:
    """复盘 R-4:runner 崩溃路径(NEEDS_REVIEW 收敛)的 finally 取证必须落盘——
    diff.patch 与 report.json 缺失会让崩溃任务无从复盘。"""
    bug = load_bug("BUG-001", BUG_ROOT)

    class _BoomModel:
        provider = "fake-replay"

        def complete(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("model exploded (实现缺陷模拟)")

    result = run_task_graph(bug, _BoomModel(), runs_root=tmp_path / "runs")  # type: ignore[arg-type]
    assert result.status == "NEEDS_REVIEW"
    assert "model exploded" in (result.error or "")

    run_dir = Path(result.run_dir)
    assert (run_dir / "diff.patch").exists()
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "NEEDS_REVIEW" and report["verdict"] == "needs_review"
