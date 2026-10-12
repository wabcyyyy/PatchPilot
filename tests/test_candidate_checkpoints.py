"""G14 C1:候选检查点存储与终局候选绑定。

覆盖三件事:
1. 门禁拒绝的候选同样保全工件与 checkpoint(gate_ok=False),rollback 复位到基线;
2. finish 只认"当前工作区 == 冻结候选":hash 漂移或冻结工件消失都拒绝 resolved;
3. verify 的真实重跑覆盖伪造的旧绿 checkpoint(fresh verification_attempt_id)。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gitops.testing import materialize_repo
from app.graph.candidate import (
    candidates_root,
    read_candidate_checkpoint,
    write_candidate_checkpoint,
)
from app.graph.nodes import TaskNodes
from app.llm.base import Model
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

BUG_ROOT = Path("bugs")
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"


def _fake_settings(**kw: object) -> SimpleNamespace:
    """只暴露节点用到的字段,测试不受本机 .env 影响。"""
    values: dict[str, object] = {
        "adaptive_branching_enabled": True,
        "token_budget": 200_000,
        "branching_min_token_reserve": 30_000,
        "branch_candidates": 2,
        "max_patch_files": 5,
        "test_timeout_seconds": 120,
        "task_timeout_seconds": 900,
        "context_window_tokens": 0,
        "context_keep_recent_turns": 6,
        "verify_double_run": False,
    }
    values.update(kw)
    return SimpleNamespace(**values)


def _model() -> Model:
    return SimpleNamespace(provider="fake-replay", complete=lambda *a, **k: None)


def _nodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TaskNodes, Path]:
    """真题目 + 真工作区(不跑 LLM;verify 用例里真跑 pytest)。"""
    template = tmp_path / "template"
    shutil.copytree(BUG_ROOT / "BUG-001" / "repo", template)
    (template / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8", newline="\n")
    ws = tmp_path / "ws"
    sha = materialize_repo(template, ws, extra_commit=False)
    bug = SimpleNamespace(
        id="BUG-001",
        root=BUG_ROOT / "BUG-001",
        repo_dir=BUG_ROOT / "BUG-001" / "repo",
        allowed_paths=["src/**"],
        test_sets={"failed": [FAILED_ID], "regression": []},
        failed_tests=[FAILED_ID],
        regression_tests=[],
        issue_text="x",
        max_rounds=5,
    )
    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: _fake_settings())
    # 受理契约缺省由 run_dir/task_spec.json 提供;直连节点时注入固定身份,
    # 否则 freeze_candidate 的 manifest 缺字段,load_candidate 读侧会判不可信
    monkeypatch.setattr(
        "app.graph.nodes.accepted_contract_hashes", lambda _run_dir: ("t-spec", "t-src")
    )
    ctx = ToolContext(
        task_id="T-CKPT",
        workspace=ws,
        baseline_commit=sha,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-CKPT"),
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
        baseline_commit=sha,
        max_rounds=5,
        max_turns=10,
    )
    nodes.ctx = ctx
    nodes.branch_model_factory = lambda i: _model()
    return nodes, ws


def _state(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "bug_id": "BUG-001",
        "issue_text": "x",
        "round_no": 2,
        "repeat_streak": 0,
        "tokens_used": 0,
        "turns": 0,
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "findings": "",
        "feedback": "",
    }
    base.update(kw)
    return base


def _fix_workspace(ws: Path) -> None:
    """工作区落正确修复(BUG-001:parse_date 未处理空白字符串)。"""
    target = ws / "src" / "dateparse.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace(
            "    if value is None:\n        return None\n",
            "    if value is None:\n        return None\n    if not value.strip():\n        return None\n",
        ),
        encoding="utf-8",
        newline="\n",
    )


def _comment_workspace(ws: Path) -> None:
    """工作区落"门禁通过但不修复问题"的合法补丁(只加注释)。"""
    target = ws / "src" / "dateparse.py"
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace(
            '    raise ValueError(f"unrecognized date format: {value!r}")\n',
            '    raise ValueError(f"unrecognized date format: {value!r}")  # noqa: 见 issue\n',
        ),
        encoding="utf-8",
        newline="\n",
    )


# ---------- ①门禁拒绝:工件与 checkpoint 保全,rollback 回基线 ----------


def test_gate_rejected_candidate_is_preserved_and_rollback_resets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nodes, ws = _nodes(monkeypatch, tmp_path)
    # 直接改测试文件制造 [files] 门禁违规(apply 节点只读工作区 diff,不经工具层)
    target = ws / "tests" / "test_dateparse.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# 门禁违规探测\n",
        encoding="utf-8",
        newline="\n",
    )

    state = _state()
    result = nodes.apply(state)  # type: ignore[arg-type]
    assert result["status"] == "PATCH_REJECTED"
    assert any("[files]" in v for v in result["gate_violations"]), result["gate_violations"]
    candidate_id = str(result["candidate_id"])
    assert candidate_id and str(result["candidate_hash"])

    # 失败现场保全:diff.patch 与 checkpoint(gate_ok=False,不冒充绿)
    assert (tmp_path / "candidates" / candidate_id / "diff.patch").is_file()
    checkpoint = read_candidate_checkpoint(tmp_path, candidate_id)
    assert checkpoint is not None
    assert checkpoint.gate_ok is False
    assert checkpoint.verify_failed_ok is False and checkpoint.verify_regression_ok is False
    assert "gates rejected" in checkpoint.note

    # rollback 单线失败路径:reset 到基线,至多一次递增,下一轮继续
    state.update(result)
    rolled = nodes.rollback(state)  # type: ignore[arg-type]
    assert rolled["status"] == "PROPOSE_PATCH" and rolled["round_no"] == 3
    # G14:被拒候选不放行续改——整体复位,候选引用全部清空(防旧 hash 误绑)
    assert rolled["continue_from_candidate_id"] == ""
    assert rolled["candidate_id"] == ""
    assert rolled["candidate_hash"] == ""
    from app.gitops.rollback import working_tree_is_clean

    assert working_tree_is_clean(ws)


# ---------- ②终局候选绑定:hash 漂移 / 工件消失都拒绝 resolved ----------


def test_finish_rejects_drifted_workspace_and_missing_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nodes, ws = _nodes(monkeypatch, tmp_path)
    _fix_workspace(ws)
    state = _state()
    applied = nodes.apply(state)  # type: ignore[arg-type]
    assert applied["status"] == "VERIFY"
    candidate_id = str(applied["candidate_id"])

    # 合法候选 + 平台验证全绿 → 绑定成立 → resolved
    state.update(applied)
    state["verify_failed_ok"] = True
    state["verify_regression_ok"] = True
    decided = nodes.finish(state)  # type: ignore[arg-type]
    assert decided["status"] == "FINISHED" and decided["outcome"] == "resolved"

    # ①hash 漂移:apply 冻结后再动工作区 → 旧候选的成功不得套到新代码上
    target = ws / "src" / "dateparse.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# 事后漂移\n", encoding="utf-8", newline="\n"
    )
    drifted = nodes.finish(state)  # type: ignore[arg-type]
    assert drifted["status"] == "VERIFY_FAILED"
    assert "workspace diff sha256" in str(drifted["error"])

    # ②补丁消失:工作区回到候选态但冻结工件被删 → load_candidate 判缺失,拒绝
    from app.gitops.rollback import reset_workspace

    reset_workspace(ws, nodes.baseline_commit)
    _fix_workspace(ws)
    shutil.rmtree(candidates_root(tmp_path) / candidate_id)
    missing = nodes.finish(state)  # type: ignore[arg-type]
    assert missing["status"] == "VERIFY_FAILED"
    assert "missing or tampered" in str(missing["error"])


# ---------- ③verify 覆盖伪造的旧绿证据 ----------


def test_verify_overwrites_forged_green_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nodes, ws = _nodes(monkeypatch, tmp_path)
    _comment_workspace(ws)  # 门禁通过但修不好 bug
    state = _state()
    applied = nodes.apply(state)  # type: ignore[arg-type]
    assert applied["status"] == "VERIFY"
    candidate_id = str(applied["candidate_id"])

    # 伪造"旧尝试全绿"证据:恢复/重试不得把它套到新代码上
    assert write_candidate_checkpoint(
        tmp_path,
        candidate_id,
        gate_ok=True,
        verify_failed_ok=True,
        verify_regression_ok=True,
        round_no=2,
        verification_attempt_id="OLD-GREEN",
    )
    forged = read_candidate_checkpoint(tmp_path, candidate_id)
    assert forged is not None and forged.verification_attempt_id == "OLD-GREEN"

    # 平台真实重跑 → 用真实结果 + fresh attempt id 覆盖
    state.update(applied)
    update = nodes.verify(state)  # type: ignore[arg-type]
    assert update["verify_failed_ok"] is False  # 补丁没修 bug,真实失败

    fresh = read_candidate_checkpoint(tmp_path, candidate_id)
    assert fresh is not None
    assert fresh.gate_ok is True
    # 伪造的全绿被真实重跑覆盖:补丁没修 bug → failed 集失败;空回归集在平台
    # 层是"跑全目录",残留失败用例同样不过 → 两个布尔都从伪造的 True 变 False
    assert fresh.verify_failed_ok is False and fresh.verify_regression_ok is False
    assert fresh.verification_attempt_id
    assert fresh.verification_attempt_id != "OLD-GREEN"
    assert fresh.verification_attempt_id == str(update["verification_attempt_id"])


# ---------- ④v2 续改:从具名候选继续修复,新候选谱系,完整重验 ----------


def test_rollback_continues_from_gate_clean_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    nodes, ws = _nodes(monkeypatch, tmp_path)
    _fix_workspace(ws)  # 合法修复(BUG-001)
    state = _state()
    applied = nodes.apply(state)  # type: ignore[arg-type]
    assert applied["status"] == "VERIFY"
    candidate_id = str(applied["candidate_id"])

    # verify 失败(模拟:候选没修全)→ rollback 判定续改而非整体复位
    state.update(applied)
    state["verify_failed_ok"] = False
    state["verify_regression_ok"] = False
    rolled = nodes.rollback(state)  # type: ignore[arg-type]
    assert rolled["status"] == "PROPOSE_PATCH" and rolled["round_no"] == 3
    assert rolled["continue_from_candidate_id"] == candidate_id
    assert "已从该候选继续修复" in str(rolled["feedback"])

    # 工作区保持候选 diff(重应用后),不是空基线——局部进展被保留
    from app.gitops.differ import working_tree_diff

    continued_diff = working_tree_diff(ws).diff_text
    assert continued_diff.strip() and "if not value.strip()" in continued_diff

    # 下一轮 apply:模型未改动 → 冻结出新轮候选身份(新 hash 身份),谱系父 = 续改起点
    state.update(rolled)
    state["round_no"] = 3
    reapplied = nodes.apply(state)  # type: ignore[arg-type]
    assert reapplied["status"] == "VERIFY"
    new_id = str(reapplied["candidate_id"])
    assert new_id and new_id != candidate_id
    manifest = json.loads(
        (candidates_root(tmp_path) / new_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["parent_candidate_id"] == candidate_id
    assert manifest["round_no"] == 3

    # 续改后完整 verify:真实重跑重新验收,旧验证结论全部失效重验
    state.update(reapplied)
    update = nodes.verify(state)  # type: ignore[arg-type]
    assert update["verify_failed_ok"] is True  # 正确修复 → 通过
    fresh = read_candidate_checkpoint(tmp_path, new_id)
    assert fresh is not None
    assert fresh.gate_ok is True and fresh.verify_failed_ok is True
    assert fresh.verification_attempt_id == str(update["verification_attempt_id"])


# ---------- ⑤有界停止:连续相同失败无新增证据 → NEEDS_REVIEW ----------


def test_bounded_stop_stops_identical_failures_without_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nodes, _ws = _nodes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.graph.nodes.get_settings", lambda: _fake_settings(max_identical_failures=3)
    )
    # 分支已用过且同失败连 3 轮 → 不再 reset 重试,收敛 NEEDS_REVIEW
    state = _state(repeat_streak=3, branching_used=True)
    rolled = nodes.rollback(state)  # type: ignore[arg-type]
    assert rolled["status"] == "NEEDS_REVIEW" and rolled["outcome"] == "needs_review"
    assert "bounded stop" in str(rolled["error"])
    # 现场仍保全(不 reset,证据原样保留)
    assert rolled["preserved_diff"] == ""

    # streak 未达门槛 → 不停止
    below = nodes.rollback(_state(repeat_streak=2, branching_used=True))  # type: ignore[arg-type]
    assert below["status"] != "NEEDS_REVIEW"

    # limit=0(关闭)→ 永不有界停止
    monkeypatch.setattr(
        "app.graph.nodes.get_settings", lambda: _fake_settings(max_identical_failures=0)
    )
    off = nodes.rollback(_state(repeat_streak=9, branching_used=True))  # type: ignore[arg-type]
    assert off["status"] != "NEEDS_REVIEW"


# ---------- 2026-10-11 合并回归修复:续改准入 / 失败反馈 / 末轮终态 ----------


def _frozen_gate_clean_candidate(nodes: TaskNodes, ws: Path) -> str:
    """工作区落修复 → 冻结候选 → checkpoint 记门禁通过(验证未全绿)。"""
    from app.graph.candidate import freeze_candidate

    _fix_workspace(ws)
    run_dir = nodes.report_dir.parent
    manifest = freeze_candidate(
        run_dir,
        ws,
        task_spec_hash="t-spec",
        source_snapshot_hash="t-src",
        baseline_commit=nodes.baseline_commit,
        round_no=2,
    )
    write_candidate_checkpoint(
        run_dir,
        manifest.candidate_id,
        gate_ok=True,
        verify_failed_ok=False,
        verify_regression_ok=True,
        round_no=2,
    )
    return manifest.candidate_id


def test_unreadable_gate_clean_candidate_falls_back_to_full_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """checkpoint 说门禁干净、但候选工件被篡改:不得从未核验目录续改,也不得把图打崩
    (修复前 reapply_candidate 抛 TaskError),而是退回整体复位并留下拒绝记录。"""
    import json

    from app.gitops.differ import working_tree_diff

    nodes, ws = _nodes(monkeypatch, tmp_path)
    candidate_id = _frozen_gate_clean_candidate(nodes, ws)
    patch = candidates_root(nodes.report_dir.parent) / candidate_id / "diff.patch"
    patch.write_text(patch.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    rolled = nodes.rollback(_state(candidate_id=candidate_id, candidate_hash="x"))  # type: ignore[arg-type]
    assert rolled["status"] == "PROPOSE_PATCH" and rolled["round_no"] == 3
    assert rolled["candidate_id"] == "" and rolled["continue_from_candidate_id"] == ""
    assert working_tree_diff(ws).diff_text.strip() == "", "整体复位:工作区回到基线"
    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    refused = [e for e in events if e["tool"] == "continue_from_candidate"]
    assert refused and refused[0]["output_summary"]["refused"] is True
    assert any(e["tool"] == "reset_workspace" for e in events)


def test_continuation_keeps_the_verify_failure_feedback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """续改时验证失败的事实(哪些测试仍失败)必须留在反馈里,续改说明接在后面。"""
    nodes, ws = _nodes(monkeypatch, tmp_path)
    candidate_id = _frozen_gate_clean_candidate(nodes, ws)
    failure = f"上一轮补丁应用后仍有失败:\n- {FAILED_ID}"

    rolled = nodes.rollback(  # type: ignore[arg-type]
        _state(candidate_id=candidate_id, candidate_hash="x", feedback=failure)
    )
    assert rolled["status"] == "PROPOSE_PATCH"
    assert rolled["continue_from_candidate_id"] == candidate_id
    feedback = str(rolled["feedback"])
    assert feedback.startswith(failure), feedback
    assert "已从该候选继续修复" in feedback


def test_bounded_stop_yields_to_round_exhaustion_on_the_last_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有界停止是"提前停":末轮同时满足时以轮数用尽(BUDGET_EXCEEDED)收尾。"""
    nodes, _ws = _nodes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.graph.nodes.get_settings", lambda: _fake_settings(max_identical_failures=3)
    )
    last = nodes.rollback(  # type: ignore[arg-type]
        _state(round_no=nodes.max_rounds, repeat_streak=3, branching_used=True)
    )
    assert last["status"] == "BUDGET_EXCEEDED", last
    early = nodes.rollback(  # type: ignore[arg-type]
        _state(round_no=nodes.max_rounds - 1, repeat_streak=3, branching_used=True)
    )
    assert early["status"] == "NEEDS_REVIEW", early
