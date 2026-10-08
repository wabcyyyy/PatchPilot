"""S03/F2:候选工件与恢复再验证。

核心缺陷(review F2,P0):恢复把工作区复位到基线后直接执行检查点的 next 节点,
next=finish 时补丁已被清掉、旧 verify 布尔仍判 resolved。修复后:
apply/verify/finish 的恢复 = 候选重应用 + 清旧结论 + 从 APPLY 完整重验;
无候选/候选篡改/身份不符一律 NEEDS_REVIEW,绝不伪成功。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from app.evals.bugset import load_bug
from app.gitops.differ import working_tree_diff
from app.gitops.testing import materialize_repo
from app.graph.builder import build_graph
from app.graph.candidate import freeze_candidate, load_candidate
from app.graph.checkpoint import make_sqlite_checkpointer
from app.graph.loop_state import load_loop_snapshot
from app.graph.nodes import TaskNodes
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.tools.tracker import Tracker
from tests.conftest import block
from tests.test_graph import BUG_ROOT, _fix_diff, _localize_script

REPO_ROOT = Path(__file__).resolve().parent.parent


def _events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _seed_finish_checkpoint(
    tmp_path: Path,
    *,
    with_candidate: bool,
    task_id: str = "T-SEED",
) -> Path:
    """造"verify 已完成、next=finish"的合法检查点(审查 F2 的复现种子)。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace"
    baseline = materialize_repo(bug.repo_dir, workspace, extra_commit=False)
    from app.gitops.patcher import apply_patch

    assert apply_patch(workspace, _fix_diff()).applied  # patcher 直用 unified diff
    candidate_id = ""
    if with_candidate:
        from app.task_spec import build_task_spec, write_task_spec_file

        write_task_spec_file(
            run_dir,
            build_task_spec(
                bug, engine="graph", arm="agent", model_provider="fake-replay", max_turns=20
            ),
        )
        manifest = freeze_candidate(
            run_dir,
            workspace,
            task_spec_hash=TaskSpecHashHelper(run_dir).spec,
            source_snapshot_hash=TaskSpecHashHelper(run_dir).source,
            baseline_commit=baseline,
            round_no=1,
        )
        candidate_id = manifest.candidate_id
    checkpointer = make_sqlite_checkpointer(run_dir / "checkpoints.sqlite")
    assert checkpointer is not None
    nodes = TaskNodes(
        bug,
        FakeLLM([]),
        workspace,
        Tracker(run_dir / "trajectory.jsonl", task_id=task_id),
        run_dir / "reports",
        bug.max_rounds,
        20,
    )
    graph = build_graph(nodes, checkpointer=checkpointer)
    state = {
        "bug_id": bug.id,
        "issue_text": bug.issue_text,
        "failed_tests": bug.failed_tests,
        "regression_tests": bug.regression_tests,
        "baseline_commit": baseline,
        "max_rounds": bug.max_rounds,
        "round_no": 1,
        "status": "VERIFY",
        "verify_failed_ok": True,  # 旧的"通过"结论——恢复绝不能沿用
        "verify_regression_ok": True,
        "baseline_failed": 1,
        "baseline_regression_ok": True,
        "changed_files": ["src/dateparse.py"],
        "gate_violations": [],
        "tokens_used": 100,
        "turns": 2,
        "deadline_epoch": 9999999999.0,
    }
    if candidate_id:
        loaded = load_candidate(run_dir, candidate_id)
        assert loaded is not None
        state["candidate_id"] = candidate_id
        state["candidate_hash"] = loaded[0].diff_sha256
    config = {"configurable": {"thread_id": task_id}}
    graph.update_state(config, state, as_node="verify")
    checkpointer.conn.close()
    return run_dir


class TaskSpecHashHelper:
    """读受理契约哈希的小助手(种子场景用)。"""

    def __init__(self, run_dir: Path) -> None:
        from app.graph.candidate import accepted_contract_hashes

        self.spec, self.source = accepted_contract_hashes(run_dir)


# ---------- F2 主复现:next=finish 无候选 → 拒绝,绝不 resolved ----------


def test_f2_resume_at_finish_without_candidate_is_rejected(tmp_path: Path) -> None:
    """next=finish 且没有冻结候选:恢复拒绝(NEEDS_REVIEW),绝不沿用旧 verify 布尔。"""
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=False)
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(
        bug, FakeLLM([]), runs_root=tmp_path, task_id="T-SEED", run_dir=run_dir, resume=True
    )
    assert result.status == "NEEDS_REVIEW", (result.status, result.error)
    assert "resume rejected" in (result.error or "")
    names = [e["tool"] for e in _events(run_dir)]
    assert "final_acceptance" not in names, "拒绝的恢复不得走进终局验收"
    # 工作区未被 this 恢复动过(拒绝前保全现场):崩溃时的补丁仍在盘上
    assert not working_tree_diff(run_dir / "workspace").is_empty


def test_f2_resume_at_finish_with_candidate_revalidates_and_resolves(tmp_path: Path) -> None:
    """有候选的 next=finish 恢复:重应用→重门禁→重测试→fresh 验证→resolved。"""
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=True)
    # loop 快照在 propose 正常收尾时已删除——候选恢复不依赖它
    assert load_loop_snapshot(run_dir, task_id="T-SEED") is None
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(
        bug, FakeLLM([]), runs_root=tmp_path, task_id="T-SEED", run_dir=run_dir, resume=True
    )
    assert result.status == "FINISHED", (result.status, result.error)
    assert result.verdict == "resolved"
    assert result.candidate_id, "resolved 必须绑定候选"
    # 磁盘 diff 与候选哈希相符(重应用真的发生了)
    loaded = load_candidate(run_dir, result.candidate_id)
    assert loaded is not None
    manifest, patch_text = loaded
    assert working_tree_diff(run_dir / "workspace").diff_text == patch_text
    assert result.candidate_hash == manifest.diff_sha256
    assert result.verification_attempt_id, "必须有本次执行的新验证尝试 id"
    names = [e["tool"] for e in _events(run_dir)]
    start = names.index("resume_from_checkpoint")
    after = names[start:]
    assert "resume_candidate_reapplied" in after
    assert "apply_gate" in after, "门禁必须重跑"
    # verify 节点的事件面:verify + 双跑复核(不是工具 run_tests——那是 Agent 的)
    assert after.count("verify") >= 1 and "verify_double_run" in after


# ---------- 真进程死亡:verify→finish 边界(os._exit) ----------


_CHILD_DIE_AT_FINISH = '''
"""崩溃方子进程:verify 完成后、finish 进入即 os._exit(9),检查点停在 next=finish。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from app.graph import nodes as nodes_mod          # noqa: E402
from app.evals.bugset import load_bug             # noqa: E402
from app.graph.runner import run_task_graph       # noqa: E402
from app.llm.fake import FakeLLM                  # noqa: E402
from tests.conftest import block                  # noqa: E402
from tests.test_graph import BUG_ROOT, _fix_diff, _localize_script  # noqa: E402

# verify 节点完成 → 检查点已落(next=finish)→ finish 方法体第一行就死:
# 没有任何 finally、连接不关、候选工件就是磁盘上唯一的补丁正本
nodes_mod.TaskNodes.finish = lambda self, state: os._exit(9)

script = _localize_script() + [
    {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
]
child = run_task_graph(
    load_bug("BUG-001", BUG_ROOT),
    FakeLLM(script),
    runs_root=sys.argv[2],
    task_id=sys.argv[3],
    run_dir=Path(sys.argv[2]) / sys.argv[3],
)
print("CHILD-DID-NOT-DIE", child.status)
'''


def test_real_process_death_at_verify_finish_boundary_recovers(tmp_path: Path) -> None:
    """spec 必测:verify→finish 边界的 os._exit 真进程死亡 → 按候选恢复并 resolved。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    task_id = "T-DIE-FINISH"
    runs_root = tmp_path / "runs"
    run_dir = runs_root / task_id
    child_script = tmp_path / "die_at_finish_child.py"
    child_script.write_text(_CHILD_DIE_AT_FINISH, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(child_script), str(REPO_ROOT), str(runs_root), task_id],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PATCHPILOT_LLM_ENABLED": "false", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 9, (proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    assert load_loop_snapshot(run_dir, task_id=task_id) is None, "快照已随 propose 收尾删除"
    events = _events(run_dir)
    candidate_events = [e for e in events if e["tool"] == "candidate_frozen"]
    assert candidate_events, "apply 入口必须已冻结候选"

    resumed = run_task_graph(
        bug,
        FakeLLM([]),
        runs_root=runs_root,
        task_id=task_id,
        run_dir=run_dir,
        resume=True,
    )
    assert resumed.status == "FINISHED", (resumed.status, resumed.error)
    assert resumed.verdict == "resolved"
    assert resumed.candidate_id == candidate_events[-1]["output_summary"]["candidate_id"]
    names = [e["tool"] for e in _events(run_dir)]
    start = names.index("resume_from_checkpoint")
    after = names[start:]
    assert "resume_candidate_reapplied" in after and "apply_gate" in after
    assert "verify_double_run" in after  # 双跑复核在恢复这次真重跑


# ---------- 候选缺失/篡改/END/取消 ----------


def test_resume_with_tampered_candidate_is_rejected(tmp_path: Path) -> None:
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=True)
    candidate_dir = next((run_dir / "candidates").iterdir())
    patch = candidate_dir / "diff.patch"
    patch.write_text(patch.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(
        bug, FakeLLM([]), runs_root=tmp_path, task_id="T-SEED", run_dir=run_dir, resume=True
    )
    assert result.status == "NEEDS_REVIEW"
    assert "tampered" in (result.error or "")


def test_resume_with_missing_candidate_dir_is_rejected(tmp_path: Path) -> None:
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=True)
    candidate_id = next((run_dir / "candidates").iterdir()).name
    bug = load_bug("BUG-001", BUG_ROOT)
    # checkpoint 指向的候选被清走 → 恢复拒绝
    import shutil

    shutil.rmtree(run_dir / "candidates" / candidate_id)
    result = run_task_graph(
        bug, FakeLLM([]), runs_root=tmp_path, task_id="T-SEED", run_dir=run_dir, resume=True
    )
    assert result.status == "NEEDS_REVIEW"
    assert "missing or tampered" in (result.error or "")


def test_resume_at_terminal_checkpoint_does_not_reexecute(tmp_path: Path) -> None:
    """END 恢复:只核对既有终态产物,不冷启动、不再调用模型、不覆盖原终态。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    task_id = "T-ENDED"
    first = run_task_graph(
        bug,
        FakeLLM(
            [
                *_localize_script(),
                {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
                {"tool": "run_tests", "args": {"test_set": "failed"}},
                {"tool": "run_tests", "args": {"test_set": "regression"}},
                {"tool": "finish", "args": {"success": True, "summary": "done"}},
            ]
        ),
        runs_root=tmp_path,
        task_id=task_id,
        run_dir=tmp_path / task_id,
    )
    assert first.status == "FINISHED"
    events_before = len(_events(tmp_path / task_id))

    class _Counting(FakeLLM):
        def complete(self, messages, tools):
            raise AssertionError("END 恢复不得再调用模型")

    second = run_task_graph(
        bug,
        _Counting([]),
        runs_root=tmp_path,
        task_id=task_id,
        run_dir=tmp_path / task_id,
        resume=True,
    )
    assert second.status == "FINISHED" and second.verdict == "resolved"
    assert "terminal checkpoint replayed" in (second.error or "")
    events = _events(tmp_path / task_id)
    assert len(events) == events_before + 1  # 只多一条 resume_terminal_replayed
    assert events[-1]["tool"] == "resume_terminal_replayed"


def test_resume_with_cancel_requested_never_resolves(tmp_path: Path) -> None:
    """取消优先:恢复过程中取消已请求 → 终局验收拒绝 resolved。"""
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=True)
    bug = load_bug("BUG-001", BUG_ROOT)
    cancel_event = threading.Event()
    cancel_event.set()
    result = run_task_graph(
        bug,
        FakeLLM([]),
        runs_root=tmp_path,
        task_id="T-SEED",
        run_dir=run_dir,
        cancel_event=cancel_event,
        resume=True,
    )
    assert result.status != "FINISHED"
    assert result.verdict != "resolved"


def test_resume_at_apply_revalidates_stale_flags(tmp_path: Path) -> None:
    """next=verify 的恢复:旧 verify True 被清、候选重应用、门禁+测试真重跑。"""
    run_dir = _seed_finish_checkpoint(tmp_path, with_candidate=True, task_id="T-APPLY")
    # 把检查点改写到 next=verify(verify 刚完成、next 本是 finish;此处模拟 next=verify
    # 的等价场景:直接用 update_state as_node="propose" 让 route 走 apply)
    from app.graph.checkpoint import make_sqlite_checkpointer

    bug = load_bug("BUG-001", BUG_ROOT)
    checkpointer = make_sqlite_checkpointer(run_dir / "checkpoints.sqlite")
    graph = build_graph(
        TaskNodes(
            bug,
            FakeLLM([]),
            run_dir / "workspace",
            Tracker(run_dir / "trajectory.jsonl", task_id="T-APPLY"),
            run_dir / "reports",
            bug.max_rounds,
            20,
        ),
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "T-APPLY"}}
    graph.update_state(config, {"status": "VERIFY"}, as_node="apply")
    checkpointer.conn.close()

    result = run_task_graph(
        bug, FakeLLM([]), runs_root=tmp_path, task_id="T-APPLY", run_dir=run_dir, resume=True
    )
    assert result.status == "FINISHED", (result.status, result.error)
    names = [e["tool"] for e in _events(run_dir)]
    start = names.index("resume_from_checkpoint")
    after = names[start:]
    assert after.count("apply_gate") >= 1
    assert after.count("verify") >= 1 and "verify_double_run" in after
