"""G13:轻量角色委派——单元测试(SPEC §G13)。

覆盖 SubtaskManager 的名额/额度/执行语义:
- 三角色声明面(工具白名单不含 delegate/ask_user;只读角色无写工作区工具);
- 未知角色/未配置模型/预算不足/名额占满 → 拒绝且是合法输出(不抛、不重试);
- 每个子任务独立会话与报告目录(不共享父 session/memory);
- 并发与次数上限(acquire_slot/acquire_bulk 同一把锁、同一组上限);
- 父取消 → TERM_CANCELLED;子崩溃 → TERM_CRASHED;
- 恶意 finish 声明不放行(repair_candidate 无 diff → 无候选身份,父工作区未动);
- 只读角色越权调用被运行期白名单挡下;
- 模型工厂读冻结脚本(fake),缺脚本/非 dict/缺文件 → None(unconfigured)。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.roles import ROLE_REGISTRY
from app.agent.subtasks import (
    TERM_CANCELLED,
    TERM_CRASHED,
    TERM_FINISHED,
    TERM_REFUSED,
    ChildResult,
    SubtaskManager,
    _subtask_ctx,
    branch_model_factory_for,
    subtask_model_factory_for,
)
from app.graph.resources import ResourceLedger
from app.llm.base import Model
from app.llm.fake import FakeLLM
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

BUG001 = Path("bugs/BUG-001")

FINISH_OK: dict[str, object] = {
    "tool": "finish",
    "args": {"success": True, "summary": "done"},
}


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        loop_settings=lambda: {
            "context_window_tokens": 0,
            "context_keep_recent_turns": 6,
            "capacity_ceiling_tokens": 0,
            "runtime_profile": "test",
        }
    )


def _ledger(token_limit: int = 200_000, share: float = 0.25) -> ResourceLedger:
    return ResourceLedger(task_id="T-SUB", token_limit=token_limit, subtask_budget_share=share)


def _real_ctx(tmp_path: Path) -> ToolContext:
    """真 git 工作区:子任务 finish 时的 working_tree_diff 需要真仓库。"""
    from app.gitops.testing import materialize_repo

    ws = tmp_path / "ws"
    sha = materialize_repo(BUG001 / "repo", ws, extra_commit=False)
    return ToolContext(
        task_id="T-SUB",
        workspace=ws,
        baseline_commit=sha,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-SUB"),
        report_dir=tmp_path / "reports",
        test_sets={
            "failed": ["tests/test_dateparse.py::test_empty_string_returns_none"],
            "regression": [],
        },
    )


def _finish_factory(role: str) -> FakeLLM:
    return FakeLLM([FINISH_OK])


_UNSET = object()


def _manager(
    tmp_path: Path,
    *,
    max_concurrency: int = 2,
    max_delegations: int = 3,
    max_turns: int = 5,
    factory: Callable[[str], Model] | object | None = _UNSET,
    ledger: ResourceLedger | None = None,
    cancel: threading.Event | None = None,
) -> SubtaskManager:
    bug = SimpleNamespace(id="BUG-001", repo_dir=BUG001 / "repo", root=BUG001)
    return SubtaskManager(
        enabled=True,
        max_concurrency=max_concurrency,
        max_delegations=max_delegations,
        max_turns=max_turns,
        model_factory=_finish_factory if factory is _UNSET else factory,  # type: ignore[arg-type]
        ledger=_ledger() if ledger is None else ledger,
        report_dir=tmp_path / "reports",
        bug=bug,
        runtime=_runtime(),
        cancel_event=cancel,
    )


def _delegate(
    manager: SubtaskManager, ctx: ToolContext, *, role: str = "researcher"
) -> ChildResult:
    return manager.delegate(
        role=role, task="确认根因", evidence_ids=["ev1"], candidate_id=None, ctx=ctx
    )


# ---------- 角色声明面 ----------


def test_roles_forbid_delegate_and_ask_user() -> None:
    """三个角色都不含 delegate/ask_user:子任务不能再发起子任务、不能停下来等真人。"""
    for name, spec in ROLE_REGISTRY.items():
        assert "delegate" not in spec.tools, name
        assert "ask_user" not in spec.tools, name


def test_readonly_roles_cannot_modify_workspace() -> None:
    for name in ("researcher", "reviewer"):
        tools = set(ROLE_REGISTRY[name].tools)
        assert "apply_patch" not in tools and "reset_workspace" not in tools, name
    reviewer = set(ROLE_REGISTRY["reviewer"].tools)
    assert "run_command" not in reviewer and "write_repro" not in reviewer
    researcher = set(ROLE_REGISTRY["researcher"].tools)
    assert "run_command" in researcher and "write_repro" in researcher  # 受控诊断
    repair = ROLE_REGISTRY["repair_candidate"]
    assert "apply_patch" in set(repair.tools)
    assert repair.isolated_workspace is True


def test_role_tools_unknown_role_raises() -> None:
    from app.agent.roles import role_tools

    with pytest.raises(KeyError):
        role_tools("not_a_role")


# ---------- 拒绝是合法输出 ----------


def test_unknown_role_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    child = _delegate(manager, _real_ctx(tmp_path), role="gopher")
    assert child.termination == TERM_REFUSED and not child.ok
    assert "unknown role" in child.error
    assert manager.children == []  # 拒绝不占名额账


def test_unconfigured_model_factory_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path, factory=None)
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_REFUSED and not child.ok
    assert "unconfigured" in child.error


def test_missing_role_script_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path, factory=lambda role: None)
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_REFUSED and not child.ok
    assert "reviewer" not in child.error and "researcher" in child.error


def test_budget_insufficient_refused(tmp_path: Path) -> None:
    """父账本先把子任务池占满 → delegate 拒绝,不发起任何请求。"""
    ledger = _ledger(token_limit=100, share=1.0)
    assert ledger.reserve_subtask(tokens=90, stage="researcher") is True
    manager = _manager(tmp_path, ledger=ledger)
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_REFUSED and not child.ok
    assert "budget insufficient" in child.error


def test_slot_exhaustion_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path, max_delegations=1)
    manager.record_child(
        ChildResult(
            role="researcher", subtask_id="sub1", ok=True, conclusion="", termination=TERM_FINISHED
        )
    )
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_REFUSED and not child.ok
    assert "slots exhausted" in child.error


# ---------- 名额与并发 ----------


def test_acquire_slot_enforces_concurrency_then_count(tmp_path: Path) -> None:
    manager = _manager(tmp_path, max_concurrency=2, max_delegations=3)
    assert manager.acquire_slot() is True
    assert manager.acquire_slot() is True
    assert manager.acquire_slot() is False  # 并发占满
    manager.release_slot()
    assert manager.acquire_slot() is True
    # children 计数到 3 后次数上限拦住
    for i in range(3):
        manager.record_child(
            ChildResult(
                role="researcher",
                subtask_id=f"sub{i}",
                ok=True,
                conclusion="",
                termination=TERM_FINISHED,
            )
        )
    assert manager.acquire_slot() is False


def test_acquire_bulk_takes_min_of_slots_and_concurrency(tmp_path: Path) -> None:
    manager = _manager(tmp_path, max_concurrency=2, max_delegations=5)
    assert manager.acquire_bulk(3) == 2  # 并发上限截到 2
    assert manager.acquire_bulk(1) == 0  # 并发占满 → 0(全拒)
    manager.release_bulk(2)
    assert manager.acquire_bulk(5) == 2  # 仍是并发上限截
    manager.release_bulk(2)
    for i in range(3):
        manager.record_child(
            ChildResult(
                role="researcher",
                subtask_id=f"sub{i}",
                ok=True,
                conclusion="",
                termination=TERM_FINISHED,
            )
        )
    assert manager.acquire_bulk(5) == 2  # 次数上限再截(min(5, 2, 2))


def test_per_subtask_budget_floor_is_one() -> None:
    ledger = _ledger(token_limit=3, share=1.0)
    manager = _manager(Path("unused"), ledger=ledger)
    assert manager.per_subtask_budget() == 1


# ---------- 会话独立性 ----------


def test_subtask_ctx_has_no_session_or_memory(tmp_path: Path) -> None:
    parent = _real_ctx(tmp_path)
    parent.session = object()  # type: ignore[assignment]
    parent.memory = object()  # type: ignore[assignment]
    sub = _subtask_ctx(
        parent,
        task_id="T-SUB-sub1",
        workspace=parent.workspace,
        baseline_commit=parent.baseline_commit,
        report_dir=tmp_path / "reports" / "sub1",
    )
    assert sub.task_id == "T-SUB-sub1"
    assert getattr(sub, "session", None) is None
    assert getattr(sub, "memory", None) is None


def test_delegate_writes_own_trajectory_and_report_dir(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_FINISHED and child.ok
    assert child.subtask_id == "sub1" and child.turns >= 1
    traj = tmp_path / "reports" / "sub1" / "trajectory.jsonl"
    assert traj.is_file()
    assert json.loads(traj.read_text(encoding="utf-8").splitlines()[-1])["tool"] == "finish"


# ---------- 取消 / 崩溃 ----------


def test_parent_cancel_terminates_cancelled(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    manager = _manager(tmp_path, cancel=cancel)
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_CANCELLED and not child.ok


class _CrashModel:
    provider = "crash"

    def complete(self, messages: object, tools: object) -> object:
        raise RuntimeError("subtask model exploded")


def test_child_crash_terminates_crashed(tmp_path: Path) -> None:
    manager = _manager(tmp_path, factory=lambda role: _CrashModel())  # type: ignore[return-value]
    child = _delegate(manager, _real_ctx(tmp_path))
    assert child.termination == TERM_CRASHED and not child.ok
    assert "RuntimeError" in child.error


# ---------- 恶意声明 / 越权 ----------


def test_repair_candidate_malicious_finish_has_no_candidate_identity(tmp_path: Path) -> None:
    """finish 声明成功但没有 diff → 没有候选身份;父工作区未被碰过。"""
    from app.gitops.differ import working_tree_diff

    manager = _manager(tmp_path)
    ctx = _real_ctx(tmp_path)
    child = _delegate(manager, ctx, role="repair_candidate")
    assert child.termination == TERM_FINISHED
    assert child.ok is True  # 模型声明成功,平台记录事实
    assert child.candidate_id is None and child.candidate_hash is None  # 无 diff → 无候选
    assert working_tree_diff(ctx.workspace).is_empty  # 父工作区干净


def test_readonly_role_runtime_blocked(tmp_path: Path) -> None:
    """声明面之外的越权调用在运行期被白名单挡下,并留痕。"""
    script = [
        {"tool": "apply_patch", "args": {"patch_text": "*** Begin Patch\n*** End Patch\n"}},
        FINISH_OK,
    ]
    manager = _manager(tmp_path, factory=lambda role: FakeLLM(script))
    child = _delegate(manager, _real_ctx(tmp_path), role="reviewer")
    assert child.termination == TERM_FINISHED
    traj = (tmp_path / "reports" / "sub1" / "trajectory.jsonl").read_text(encoding="utf-8")
    assert "not allowed in phase" in traj


# ---------- 模型工厂(冻结脚本) ----------


def test_subtask_model_factory_reads_frozen_scripts(tmp_path: Path) -> None:
    bug = SimpleNamespace(root=tmp_path)
    replay = tmp_path / "replay"
    replay.mkdir(parents=True)
    (replay / "subtasks.json").write_text(
        json.dumps({"researcher": [FINISH_OK]}), encoding="utf-8", newline="\n"
    )
    factory = subtask_model_factory_for(bug, "fake", SimpleNamespace())
    assert factory is not None
    assert isinstance(factory("researcher"), FakeLLM)
    assert factory("reviewer") is None  # 缺该角色脚本 → unconfigured
    (replay / "subtasks.json").write_text("[]", encoding="utf-8", newline="\n")
    assert subtask_model_factory_for(bug, "fake", SimpleNamespace()) is None  # 非 dict
    (replay / "subtasks.json").unlink()
    assert subtask_model_factory_for(bug, "fake", SimpleNamespace()) is None  # 缺文件


def test_branch_model_factory_reads_frozen_scripts(tmp_path: Path) -> None:
    bug = SimpleNamespace(root=tmp_path)
    replay = tmp_path / "replay"
    replay.mkdir(parents=True)
    (replay / "graph-branches.json").write_text(
        json.dumps({"0": [FINISH_OK], "1": []}), encoding="utf-8", newline="\n"
    )
    factory = branch_model_factory_for(bug, "fake", SimpleNamespace())
    assert isinstance(factory(0), FakeLLM)
    assert isinstance(factory(1), FakeLLM)  # 空脚本 = 立刻声明失败,不越界
    (replay / "graph-branches.json").unlink()
    assert branch_model_factory_for(bug, "fake", SimpleNamespace()) is None
