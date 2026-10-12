"""G16-C2:G07 收口——执行与恢复从**受理时冻结的** TaskSpec 重建有效策略。

SPEC G16:"受理与恢复均从已冻结 TaskSpec 重建有效策略,不能仅比较 profile 名称;
同名 profile 下窗口/输出上限/价格/权限发生环境漂移仍沿用原契约,旧身份不完整时按
兼容约定处理。"这里钉四层:

1. `TaskRuntime.from_frozen` 往返重建(窗口/输出上限/能力声明逐字复原)+ 策略指纹
   能区分"同名 profile、数值漂移";
2. 旧契约(effective_policy 没有 runtime_profile 键)读不出 ⇒ None,调用方按现状
   重建(兼容约定);策略在场但内容非法 ⇒ 构造校验当场拒绝,不静默换一份;
3. `adopt_runtime` 回填运行 profile 时,已持有构造期 runtime 的 subtask_manager 一并
   换掉;ToolContext 资源上限从冻结策略读,环境漂移不改变已受理任务的边界;
4. 绑定校验的数值牙齿:快照带 runtime_policy_hash 而当前指纹不同 ⇒ 待审(只比名字
   发现不了);旧快照没带指纹 ⇒ 不参与比较;整链集成:环境漂移后跑图,轨迹的
   runtime_profile 事件仍是受理时冻结的那组数字。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.runtime import TaskRuntime
from app.config import get_settings
from app.errors import TaskError
from app.evals.bugset import load_bug, load_replay_script
from app.graph.loop_state import LoopSnapshot
from app.graph.nodes import TaskNodes
from app.graph.resume import binding_mismatch
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.task_spec import build_task_spec, write_task_spec_file
from app.tools.tracker import Tracker
from tests.test_graph import _fix_diff, _localize_script, _propose_script

BUG_ROOT = Path("bugs")


def _settings(**overrides: object) -> SimpleNamespace:
    """构建 TaskRuntime/profile_from_settings 需要的最小 Settings 替身。"""
    base: dict[str, object] = {
        "agent_runtime_profile": "repair_v2",
        "llm_model": "test-model",
        "llm_max_tokens": 4096,
        "token_budget": 200_000,
        "context_window_tokens": 8_000,
        "context_keep_recent_turns": 2,
        "capacity_margin_tokens": 0,
        "model_context_tokens": 32_000,
        "model_capacity_source": "operator_config",
        "model_capabilities": "",
        "model_declared_output_ceiling": 8192,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_from_frozen_roundtrip_and_drift_fingerprint() -> None:
    accepted = TaskRuntime.build(_settings(), profile_name="repair_v2")
    rebuilt = TaskRuntime.from_frozen({"runtime_profile": accepted.as_dict()})
    assert rebuilt is not None
    assert rebuilt.as_dict() == accepted.as_dict()
    assert rebuilt.policy_hash == accepted.policy_hash

    # 同名 profile、数值漂移(输出上限变了)⇒ 指纹不同:绑定校验不靠名字也能拦
    drifted = TaskRuntime.build(_settings(llm_max_tokens=2048), profile_name="repair_v2")
    assert drifted.profile_name == accepted.profile_name
    assert drifted.policy_hash != accepted.policy_hash

    # legacy_v1(capacity_checked=False)同样可往返
    legacy = TaskRuntime.build(_settings(agent_runtime_profile="legacy_v1"))
    legacy_rebuilt = TaskRuntime.from_frozen({"runtime_profile": legacy.as_dict()})
    assert legacy_rebuilt is not None
    assert legacy_rebuilt.as_dict() == legacy.as_dict()


def test_from_frozen_legacy_contract_and_illegal_content() -> None:
    # 兼容约定:G07 之前的旧契约没有 runtime_profile 键 ⇒ None(调用方按现状重建)
    assert TaskRuntime.from_frozen(None) is None
    assert TaskRuntime.from_frozen({}) is None
    assert TaskRuntime.from_frozen({"runtime_profile": "legacy_v1"}) is None
    assert TaskRuntime.from_frozen({"runtime_profile": {}}) is None
    # 策略在场但内容非法:重建不放松构造校验,当场拒绝(不静默换一份)
    with pytest.raises(TaskError, match="unknown capacity source"):
        TaskRuntime.from_frozen(
            {
                "runtime_profile": {
                    "profile_name": "repair_v2",
                    "model": {"capacity_source": "bogus"},
                }
            }
        )


class _ManagerStub:
    def __init__(self) -> None:
        self.runtime = None  # type: ignore[assignment]


def _make_nodes(tmp_path: Path) -> TaskNodes:
    """最小可运行 TaskNodes:真 bug 目录、内存轨迹、无检查点。"""
    return TaskNodes(
        bug=load_bug("BUG-001", BUG_ROOT),
        model=FakeLLM([]),
        workspace=tmp_path / "ws",
        tracker=Tracker(None),
        report_dir=tmp_path / "reports",
        max_rounds=3,
        max_turns=20,
    )


def test_adopt_runtime_propagates_and_ctx_uses_frozen_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_patch_files", 7)
    policy = {
        "max_patch_files": 3,
        "max_read_lines": 100,
        "max_search_results": 5,
        "test_timeout_seconds": 60,
        "execution_backend": "local",
    }
    frozen = TaskRuntime.from_frozen({"runtime_profile": TaskRuntime.build(_settings()).as_dict()})
    assert frozen is not None

    nodes = _make_nodes(tmp_path)
    manager = _ManagerStub()
    nodes.subtask_manager = manager  # 模拟 __post_init__ 已按构造期 runtime 建好
    nodes.adopt_runtime(frozen, policy)
    assert nodes.runtime is frozen
    assert nodes.frozen_policy == policy
    assert manager.runtime is frozen  # 子任务编排器不得留在构造期那份上

    state = {"bug_id": "T-FROZEN"}
    ctx = nodes._build_ctx(state)
    assert ctx.max_patch_files == 3  # 冻结值,不是漂移后的 7
    assert ctx.max_read_lines == 100

    # 兼容约定:没有冻结策略的节点(旧契约/直接构造)按 Settings 读
    plain = _make_nodes(tmp_path / "plain")
    assert plain._build_ctx(state).max_patch_files == 7


def test_binding_policy_hash_mismatch_and_old_snapshot_skip() -> None:
    session = SimpleNamespace(session_id="s1")
    memory = SimpleNamespace(version=1, state_hash=lambda: "m1")

    def _snapshot(policy_hash: str) -> LoopSnapshot:
        return LoopSnapshot(
            stage="LOCALIZE",
            round_no=1,
            turn_no=1,
            task_id="T",
            session_id="s1",
            runtime_profile="repair_v2",
            runtime_policy_hash=policy_hash,
        )

    runtime = SimpleNamespace(profile_name="repair_v2", policy_hash="B" * 64)
    # 名字相同、数值指纹不同 ⇒ 待审:只比 profile 名发现不了这种漂移
    mismatched, readings = binding_mismatch(_snapshot("A" * 64), session, memory, runtime)
    assert "runtime_policy_hash" in mismatched
    assert "runtime_profile" not in mismatched
    assert readings["snapshot"]["runtime_policy_hash"] == "A" * 12

    # 旧快照没带指纹 ⇒ 不参与比较(兼容约定,不伪造迁移失败)
    mismatched_old, _ = binding_mismatch(_snapshot(""), session, memory, runtime)
    assert mismatched_old == []


def test_graph_run_uses_frozen_policy_under_environment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整链:受理时冻结 capacity=32000 的 repair_v2 契约,运行环境漂移到 16000 ——
    轨迹里的 runtime_profile 必须仍是受理时的那组数字,而不是漂移后的重建值。"""
    monkeypatch.setattr(get_settings(), "model_context_tokens", 32000)
    monkeypatch.setattr(get_settings(), "model_capacity_source", "operator_config")
    bug = load_bug("BUG-001", BUG_ROOT)
    spec = build_task_spec(
        bug,
        engine="graph",
        arm="agent",
        model_provider="fake-replay",
        max_turns=20,
        replay=load_replay_script(bug, kind="graph"),
        profile="repair_v2",
    )
    accepted_window = spec.effective_policy["runtime_profile"]["window"]
    assert accepted_window["capacity_tokens"] == 32000

    run_dir = tmp_path / "runs" / "drift-task"
    run_dir.mkdir(parents=True)
    write_task_spec_file(run_dir, spec)

    # 环境漂移:同名 profile(repair_v2),声明容量被改小 —— 已受理任务不得跟着变
    monkeypatch.setattr(get_settings(), "model_context_tokens", 16000)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", run_dir=run_dir)
    assert result.verdict == "resolved", (result.status, result.error)

    events = [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    profiles = [e for e in events if e.get("tool") == "runtime_profile"]
    assert len(profiles) == 1
    summary = profiles[0]["output_summary"]
    assert summary["profile_name"] == "repair_v2"
    assert summary["window"]["capacity_tokens"] == 32000  # 冻结值,不是 16000
    assert summary["window"]["adopted_tokens"] == accepted_window["adopted_tokens"]
