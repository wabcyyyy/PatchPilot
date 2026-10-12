"""G16-C3:report.json v2 补全段与 Markdown 三态呈现。

SPEC G16:"`report.json` v2 记录验收清单、候选、验证、资源来源、模型能力、命令、
子任务、输入事件和运行身份;Markdown 报告显示'已验证/未运行/不确定'各项。"这里钉:

1. collect_run_evidence 从 run_dir 工件如实收集(验收清单两态、gate 拒绝的候选也
   列出、命令截断、输入事件、运行身份含指纹与 served_model 去重);
2. 工件缺失(旧任务/提前终止)落空结构,不冒充"没有验收/没有候选";
3. 两引擎共用 _write_report 落盘点自动携带 evidence;Markdown 把三态翻成
   已验证/未运行/不确定(原值保留),候选/命令/输入/身份各节按在场与否渲染。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.runtime import TaskRuntime
from app.api.report import render_markdown
from app.evals.bugset import load_bug
from app.evals.report_evidence import (
    COMMAND_EVENT_CAP,
    collect_run_evidence,
)
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from tests.test_graph import _fix_diff, _localize_script, _propose_script

BUG_ROOT = Path("bugs")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _synthetic_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "acceptance_proposal.json",
        {
            "proposal_id": "prop-1",
            "content_hash": "a" * 64,
            "revision": 1,
            "failed_tests": ["tests/test_x.py::test_a"],
            "regression_tests": ["tests/test_x.py::test_b"],
        },
    )
    _write_json(
        run_dir / "acceptance_manifest.json",
        {
            "manifest_hash": "b" * 64,
            "proposal_id": "prop-1",
            "revision": 1,
            "approved_by": "trusted_user",
            "approved_at": "2026-10-12T00:00:00Z",
            "failed_tests": ["tests/test_x.py::test_a"],
            "regression_tests": ["tests/test_x.py::test_b"],
        },
    )
    _write_json(
        run_dir / "candidates" / "cand-lost" / "manifest.json",
        {
            "candidate_id": "cand-lost",
            "diff_sha256": "c" * 64,
            "round_no": 1,
            "parent_candidate_id": None,
            "created_at": "2026-10-12T00:01:00Z",
        },
    )
    # gate 拒绝的候选:checkpoint 如实 gate_ok=False,报告不得把它藏起来
    _write_json(
        run_dir / "candidates" / "cand-lost" / "checkpoint.json",
        {
            "candidate_id": "cand-lost",
            "version": 1,
            "round_no": 1,
            "gate_ok": False,
            "verify_failed_ok": False,
            "verify_regression_ok": False,
            "verification_attempt_id": "att-1",
        },
    )
    policy = {"runtime_profile": TaskRuntime.build(_fake_settings()).as_dict()}
    _write_json(
        run_dir / "task_spec.json",
        {
            "task_spec_hash": "d" * 64,
            "platform_commit": "3fdc8e7",
            "prompts_hash": "e" * 64,
            "effective_policy": {**policy, "agent_runtime_profile": "repair_v2"},
        },
    )
    trajectory = [
        {"tool": "tool_started", "round_no": 1, "state": "PROPOSE_PATCH"},
        {
            "tool": "run_command",
            "round_no": 1,
            "state": "PROPOSE_PATCH",
            "input_payload": {"profile_id": "pytest_collect"},
            "duration_ms": 120,
            "error": "",
        },
        {
            "tool": "llm",
            "round_no": 1,
            "output_summary": {"served_model": "gpt-x", "prompt_tokens": 10},
        },
        {
            "tool": "llm",
            "round_no": 2,
            "output_summary": {"served_model": "gpt-x"},
        },
    ]
    (run_dir / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(e) for e in trajectory), encoding="utf-8"
    )
    _write_json(
        run_dir / "interaction_state.json",
        {
            "schema_version": 1,
            "task_id": "T",
            "revision": 2,
            "questions": [
                {
                    "question_id": "q1",
                    "question": "选哪个验收口径?",
                    "status": "answered",
                    "answer_text": "按 B 口径",
                    "answer_source": "user",
                }
            ],
            "steers": [{"text": "优先看 src/util.py", "consumed_at": "2026-10-12T00:02:00Z"}],
        },
    )
    return run_dir


def _fake_settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        agent_runtime_profile="repair_v2",
        llm_model="test-model",
        llm_max_tokens=4096,
        token_budget=0,
        context_window_tokens=0,
        context_keep_recent_turns=1,
        capacity_margin_tokens=0,
        model_context_tokens=32000,
        model_capacity_source="operator_config",
        model_capabilities="",
        model_declared_output_ceiling=8192,
    )


def test_collect_run_evidence_from_artifacts(tmp_path: Path) -> None:
    run_dir = _synthetic_run_dir(tmp_path)
    evidence = collect_run_evidence(run_dir)

    acceptance = evidence["acceptance"]
    assert acceptance["proposed"] and acceptance["frozen"]
    assert acceptance["proposal_id"] == "prop-1"
    assert acceptance["manifest_hash"] == "b" * 64
    assert acceptance["approved_by"] == "trusted_user"

    candidates = evidence["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "cand-lost"
    assert candidates[0]["gate_ok"] is False  # 被拒候选如实列出
    assert candidates[0]["verification_attempt_id"] == "att-1"

    commands = evidence["commands"]
    assert len(commands) == 1
    assert commands[0]["profile_id"] == "pytest_collect"

    inputs = evidence["inputs"]
    assert inputs["revision"] == 2
    assert inputs["questions"][0]["status"] == "answered"
    assert inputs["steers"][0]["consumed"] is True

    identity = evidence["run_identity"]
    assert identity["task_spec_hash"] == "d" * 64
    assert identity["agent_profile"] == "repair_v2"
    assert identity["profile_name"] == "repair_v2"
    assert len(identity["runtime_policy_hash"]) == 64
    assert identity["served_models"] == ["gpt-x"]  # 去重


def test_collect_run_evidence_empty_dir_is_honest(tmp_path: Path) -> None:
    """旧任务/取消路径:工件缺失落空结构,不冒充。"""
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    evidence = collect_run_evidence(run_dir)
    assert evidence["acceptance"] == {
        "proposed": False,
        "frozen": False,
        "proposal_id": "",
        "proposal_hash": "",
        "manifest_hash": "",
        "revision": 0,
        "failed_tests": [],
        "regression_tests": [],
        "approved_by": "",
        "approved_at": "",
    }
    assert evidence["candidates"] == []
    assert evidence["commands"] == []
    assert evidence["inputs"] == {"revision": 0, "questions": [], "steers": []}
    assert evidence["run_identity"]["task_spec_hash"] == ""
    assert evidence["run_identity"]["runtime_policy_hash"] == ""


def test_command_events_capped(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events = [
        {"tool": "run_command", "round_no": i, "state": "S", "input_payload": {}}
        for i in range(COMMAND_EVENT_CAP + 10)
    ]
    (run_dir / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    assert len(collect_run_evidence(run_dir)["commands"]) == COMMAND_EVENT_CAP


def test_markdown_tristate_and_evidence_sections(tmp_path: Path) -> None:
    run_dir = _synthetic_run_dir(tmp_path)
    result = {
        "task_id": "T-MD",
        "bug_id": "BUG-001",
        "verdict": "resolved",
        "status": "FINISHED",
        "engine": "graph",
        "model_provider": "fake-replay",
        "report_schema_version": 2,
        "validation_status": "passed",
        "gate_status": "not_run",
        "resource_status": "inconclusive",
        "changed_files": ["src/dateparse.py"],
        "run_dir": str(run_dir),
        "evidence": collect_run_evidence(run_dir),
    }
    md = render_markdown(result)
    # 三态显式:已验证/未运行/不确定 + 原值保留
    assert "已验证(passed)" in md
    assert "未运行(not_run)" in md
    assert "不确定(inconclusive)" in md
    # 证据各节
    assert "## 验收清单" in md and "已冻结(用户批准)" in md
    assert "## 候选" in md and "门禁拒绝" in md and "cand-lost" in md
    assert "## 受控命令(共 1 条)" in md and "pytest_collect" in md
    assert "## 输入事件" in md and "已回答(user)" in md and "已消费" in md
    assert "## 运行身份" in md and "repair_v2" in md


def test_markdown_old_report_without_evidence_renders_unchanged_sections() -> None:
    """旧报告(无 evidence 键)不渲染证据节,也不冒充三态之外的东西。"""
    md = render_markdown(
        {
            "task_id": "T-OLD",
            "verdict": "failed",
            "status": "VERIFY_FAILED",
            "validation_status": "failed",
            "gate_status": "passed",
            "resource_status": "within_budget",
        }
    )
    assert "已验证(failed)" in md and "已验证(passed)" in md and "已验证(within_budget)" in md
    assert "## 候选" not in md and "## 验收清单" not in md and "## 运行身份" not in md


def test_graph_report_carries_evidence(tmp_path: Path) -> None:
    """整链:图任务收尾时 _write_report 自动带 evidence(候选/身份与工件一致)。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    assert result.verdict == "resolved"
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    evidence = report["evidence"]
    assert evidence["run_identity"]["task_spec_hash"]
    assert evidence["run_identity"]["profile_name"]
    assert report["candidate_id"] in [c["candidate_id"] for c in evidence["candidates"]]
    md = render_markdown(report)
    assert "## 候选" in md and "## 运行身份" in md
