"""消融对照臂测试(零网络):循环组成、执行反馈缺失、预算记账与标签贯通。

对照臂的价值全在"与默认臂只差两处机制"这个断言上,所以用例都围着它转:
工具集里不得有 run_tests、两阶段的 allowed_tools/token 预算必须按graph同形串接、
report.json 必须自带 arm 标签(否则批次无法区分两臂)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.errors import BudgetError
from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.evals.metrics import load_run
from app.evals.report import repro_commands
from app.evals.run_single import main as run_single_main
from app.evals.single_shot import ONE_SHOT_TOOLS, one_shot_agent
from app.graph.nodes import READ_TOOLS
from app.graph.plain_loop import LoopOutcome
from app.llm.fake import FakeLLM

BUG_ROOT = Path("bugs")


def _events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def test_one_shot_arm_resolves_with_the_same_replay_script(tmp_path: Path) -> None:
    """同一份 gold 回放脚本,去掉执行反馈后仍能 resolved——并且测试从未真的跑过。

    用 graph 那份脚本:它本身就是"先 finish 定位结论、再补丁"的两相位形状,与对照臂
    的循环形状同构;plain 那份是单相位(没有中间 finish),喂给两相位循环会提前耗尽。
    脚本里 apply_patch 之后原样保留 run_tests 两步:默认臂会执行它们,对照臂必须
    把它们挡在门外。判定权仍然只在平台 VERIFY 手里(两臂同一条代码路径)。
    """
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug, kind="graph"))
    result = run_task(bug, model, runs_root=tmp_path / "runs", arm="one_shot", agent=one_shot_agent)

    assert result.verdict == "resolved" and result.status == "FINISHED"
    assert result.arm == "one_shot"
    assert result.changed_files == ["src/dateparse.py"]
    assert result.verify_failed_ok and result.verify_regression_ok

    events = _events(Path(result.run_dir))
    states = {e.get("state") for e in events}
    # plain 引擎的 VERIFY 不经轨迹记录(那是 graph 的节点),判定证据看 report 字段
    assert {"LOCALIZE", "PROPOSE_ONE_SHOT"} <= states
    # 执行反馈被消融:run_tests 只能以"被拒"的形态出现,一次都没真执行
    run_tests_events = [e for e in events if e["tool"] == "run_tests"]
    assert run_tests_events, "脚本里的 run_tests 尝试应当留在轨迹里"
    assert all(e.get("error") for e in run_tests_events)


def test_default_arm_still_runs_the_plain_loop_unchanged(tmp_path: Path) -> None:
    """接缝不得改变默认臂行为:同一脚本、同一判定,且 arm 标签为 agent。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task(bug, FakeLLM(load_replay_script(bug)), runs_root=tmp_path / "runs")

    assert result.verdict == "resolved"
    assert result.arm == "agent"
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["arm"] == "agent"
    assert report["provenance"]["arm"] == "agent"
    # 默认臂里 run_tests 是真实执行过的(与对照臂的对照组)
    executed = [e for e in _events(Path(result.run_dir)) if e["tool"] == "run_tests"]
    assert any(not e.get("error") for e in executed)


def test_repro_command_carries_the_arm(tmp_path: Path) -> None:
    """对照臂批次的复现命令必须带 --arm,否则给出的是"会跑成默认臂"的假命令。"""
    run_dir = tmp_path / "runs" / "one_shot"
    report = {
        "task_id": "BUG-001-1",
        "bug_id": "BUG-001",
        "verdict": "failed",
        "status": "VERIFY_FAILED",
        "model_provider": "openai",
        "model_name": "deepseek-flash",
        "engine": "plain",
        "arm": "one_shot",
        "rounds": 1,
        "turns": 5,
        "tokens_used": 1000,
        "duration_ms": 1,
        "changed_files": [],
        "gate_violations": [],
        "verify_failed_ok": False,
        "verify_regression_ok": True,
        "provenance": {"model_provider": "openai", "engine": "plain", "arm": "one_shot"},
    }
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

    row = load_run(run_dir)
    assert row is not None
    cmds = repro_commands(tmp_path / "runs", [row], "docs/eval.md")
    assert any("--engine plain --arm one_shot" in c for c in cmds)


def test_one_shot_agent_tool_sets_and_budget_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """两阶段的组成:定位用 READ_TOOLS,补丁用 ONE_SHOT_TOOLS 且无 run_tests;
    补丁阶段的 token 余量按任务级扣减(与 graph 的 N-11 口径一致)。"""
    monkeypatch.setenv("PATCHPILOT_TOKEN_BUDGET", "50000")
    calls: list[dict[str, Any]] = []

    def _fake_loop(ctx: Any, model: Any, text: str, **kwargs: Any) -> LoopOutcome:
        calls.append({"text": text, **kwargs})
        index = len(calls)
        if index == 1:
            return LoopOutcome(
                success=True,
                summary="根因在 src/a.py",
                turns=3,
                tokens_used=1200,
                patch_applied=False,
                finish_declared=True,
                tokens_prompt=1000,
                tokens_completion=200,
            )
        return LoopOutcome(
            success=True,
            summary="已修",
            turns=2,
            tokens_used=300,
            patch_applied=True,
            finish_declared=True,
            tokens_prompt=250,
            tokens_completion=50,
        )

    monkeypatch.setattr("app.evals.single_shot.run_plain_loop", _fake_loop)
    bug = load_bug("BUG-001", BUG_ROOT)

    outcome = one_shot_agent(None, None, bug, max_turns=9)  # type: ignore[arg-type]

    assert len(calls) == 2
    assert calls[0]["allowed_tools"] == READ_TOOLS
    assert calls[0]["max_turns"] == 9
    assert "run_tests" not in calls[1]["allowed_tools"]
    assert calls[1]["allowed_tools"] == ONE_SHOT_TOOLS
    # 两阶段与真实臂各段的轮次上界必须相同:消融变量只有"无 run_tests + 无重试",
    # 私自削短补丁段会把臂变成"残臂"(2026-10-07 的 0/7 空结果就是这么造出来的)
    assert calls[1]["max_turns"] == calls[0]["max_turns"] == 9
    # 定位阶段的用量必须从补丁阶段的余量里扣掉
    assert get_settings().token_budget == 50000
    assert calls[1]["token_budget"] == 50000 - 1200
    # 定位结论进补丁提示,且提示里不再出现"用 run_tests 验证"
    assert "根因在 src/a.py" in calls[1]["text"]
    assert "run_tests(test_set" not in calls[1]["text"]

    assert outcome.turns == 5
    assert outcome.tokens_used == 1500
    assert outcome.tokens_prompt == 1250
    assert outcome.tokens_completion == 250
    assert outcome.patch_applied and outcome.finish_declared


def test_patch_phase_budget_error_keeps_localize_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """补丁阶段超预算时,定位阶段已烧的 token/turns 必须记回任务级账本(N-11 同源)。"""
    localize = LoopOutcome(
        success=True,
        summary="根因",
        turns=4,
        tokens_used=900,
        patch_applied=False,
        finish_declared=True,
        tokens_prompt=800,
        tokens_completion=100,
    )

    def _blow_on_second(ctx: Any, model: Any, text: str, **kwargs: Any) -> LoopOutcome:
        if kwargs.get("state_label") == "LOCALIZE":
            return localize
        exc = BudgetError("agent loop tokens exceed budget")
        exc.tokens_spent = 100  # type: ignore[attr-defined]
        exc.tokens_prompt = 90  # type: ignore[attr-defined]
        exc.tokens_completion = 10  # type: ignore[attr-defined]
        exc.turns = 1  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr("app.evals.single_shot.run_plain_loop", _blow_on_second)
    bug = load_bug("BUG-001", BUG_ROOT)

    with pytest.raises(BudgetError) as info:
        one_shot_agent(None, None, bug)  # type: ignore[arg-type]

    assert info.value.tokens_spent == 1000
    assert info.value.tokens_prompt == 890
    assert info.value.tokens_completion == 110
    assert info.value.turns == 5


def test_graph_engine_accepts_the_arm_as_same_engine_policy(tmp_path: Path) -> None:
    """S10a(取代旧钉子"graph 拒绝 one_shot"):graph 两臂同引擎对照现在是受控支持——
    one_shot 以策略对象进同一状态机(F8:跨引擎对照混杂的修法),CLI 不再拒绝。"""
    argv = [
        "--bug",
        "BUG-001",
        "--model",
        "fake",
        "--engine",
        "graph",
        "--arm",
        "one_shot",
        "--out",
        str(tmp_path / "one-shot-graph"),
    ]
    # 空回放脚本只影响判定结果(FAILED),不改变"策略被接受并真实执行"这一事实
    proc_info = run_single_main(argv)
    assert proc_info in (0, 1)  # resolved=0 / 非 resolved=1,都不再是 CLI 用法错误


def test_block_protocol_still_required_in_the_arm(tmp_path: Path) -> None:
    """对照臂不放宽补丁形态:提交 unified diff 依旧被拒,判定仍由平台给出。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    unified = (
        "--- a/src/dateparse.py\n"
        "+++ b/src/dateparse.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def parse_date(value):\n"
        "-    return None\n"
        "+    return value\n"
    )
    script = [
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "finish", "args": {"success": True, "summary": "根因在 parse_date"}},
        {"tool": "apply_patch", "args": {"patch_text": unified}},
        {"tool": "finish", "args": {"success": True, "summary": "交了 unified diff"}},
    ]
    result = run_task(
        bug, FakeLLM(script), runs_root=tmp_path / "runs", arm="one_shot", agent=one_shot_agent
    )

    assert result.verdict == "failed"
    assert not result.changed_files


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
