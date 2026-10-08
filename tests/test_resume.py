"""M6 崩溃恢复的行为证明:循环级续跑 → 图级续跑 → 启动重入队。

分两层与 app/graph/loop_state.py(A 级工作记忆)、app/graph/resume.py + runner
(B 级图位置)对应。这里钉的是**安全前提**而不是返回值:工作区必须复位、门禁必须
真跑、轮次上限不得重授、无快照路径必须与旧行为逐字相同——恢复一旦"看起来能跑"
但复用崩溃前的判断或偷偷多给轮次,比没有恢复更糟。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.service import TaskService
from app.config import get_settings
from app.errors import BudgetError, TaskCancelled
from app.evals.bugset import load_bug
from app.evals.driver import TaskResult
from app.gitops.differ import working_tree_diff
from app.gitops.rollback import working_tree_is_clean
from app.gitops.snapshot import create_workspace
from app.gitops.testing import materialize_repo
from app.graph.loop_state import (
    SNAPSHOT_FILENAME,
    LoopSnapshot,
    load_loop_snapshot,
    save_loop_snapshot,
)
from app.graph.nodes import TaskNodes
from app.graph.plain_loop import run_plain_loop
from app.graph.resume import prepare_resume
from app.graph.runner import run_task_graph
from app.llm.base import AssistantTurn, ToolCall
from app.llm.fake import FakeLLM
from app.storage.repository import TERMINAL_STATUSES, Repository
from app.tools.base import ToolContext
from app.tools.tracker import Tracker
from tests.conftest import block
from tests.test_graph import _fix_diff, _localize_script

BUG_ROOT = Path("bugs")
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]
MARKER = "CRASH-PARTIAL"
# BUG-001 修复补丁的**前置锚点行**(见 app/gitops/blockpatch 的上下文锚定):
# 改掉它,崩溃前那份补丁就会让续跑的 apply_patch 必然 anchor_not_found——
# 于是"续跑成功"本身就等于"工作区真的被复位过"
ANCHOR_LINE = '    """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""'


def _mark_partial(text: str) -> str:
    return text.replace(ANCHOR_LINE, f'    """{MARKER}:崩到一半的注释。"""')


@pytest.fixture()
def ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    """真工作区 + 落盘轨迹:快照必须与 trajectory.jsonl 同目录。"""
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    return ToolContext(
        task_id="T-RESUME-LOOP",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-RESUME-LOOP"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": REGRESSION_IDS},
    )


def _read_turn(turn: int) -> AssistantTurn:
    return AssistantTurn(
        content=f"第 {turn} 轮读",
        tool_calls=[
            ToolCall(id=f"c{turn}", name="read_file", arguments={"path": "src/dateparse.py"})
        ],
        usage_tokens=100,
        prompt_tokens=60,
        completion_tokens=40,
    )


def _finish_turn() -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[
            ToolCall(id="cfin", name="finish", arguments={"success": True, "summary": "好了"})
        ],
        usage_tokens=100,
        prompt_tokens=60,
        completion_tokens=40,
    )


class _ProcessDied(BaseException):
    """进程死亡的代理异常。

    必须绕过 `except Exception`:nodes 的 propose/localize 与 runner 都把普通异常
    收敛成 NEEDS_REVIEW **终态**——真崩溃是进程没了,那些收敛根本没机会跑,
    检查点里才会留下"被打断的节点"。用 BaseException 才能造出这个形态。
    """


class _ScriptedModel:
    """按预置回合应答;列表项是异常实例时抛出(模拟进程在模型调用处死掉)。"""

    provider = "scripted"

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append([dict(m) for m in messages])
        step = self.turns[min(len(self.seen), len(self.turns)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


class _CrashAfter(FakeLLM):
    """回放脚本到第 limit 次请求为止,之后按"进程死亡"退出。"""

    def __init__(self, script: list[dict], limit: int) -> None:
        super().__init__(script)
        self._limit = limit
        self.calls = 0

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls > self._limit:
            raise _ProcessDied("process died mid-turn")
        return super().complete(messages, tools)


class _StubGraph:
    """只提供 get_state 的图替身:读侧契约是 (values, next, metadata) 三元组。"""

    def __init__(self, values: dict[str, Any], next_node: str, step: int = 3) -> None:
        self._snapshot = SimpleNamespace(
            values=values,
            next=(next_node,),
            config={"configurable": {"checkpoint_id": "cp-stub-1"}},
            metadata={"step": step},
        )

    def get_state(self, config: dict[str, Any]) -> SimpleNamespace:
        return self._snapshot


def _partial_diff() -> str:
    """崩溃前打了一半的补丁:改的正是正确修复所依赖的锚点行。"""
    tmp = Path(tempfile.mkdtemp(prefix="resume-partial-"))
    try:
        src = tmp / "src"
        materialize_repo(BUG_ROOT / "BUG-001" / "repo", src)
        ws = tmp / "ws"
        create_workspace(src, ws)
        target = ws / "src" / "dateparse.py"
        target.write_text(
            _mark_partial(target.read_text(encoding="utf-8")), encoding="utf-8", newline="\n"
        )
        return working_tree_diff(ws).diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "trajectory.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------- A 级:循环工作记忆 ----------


def test_resume_continues_from_crash_turn(ctx: ToolContext) -> None:
    """崩溃在第 3 轮:快照停在第 2 轮;续跑从第 3 轮起、收到的是同一份历史、token 累计。"""
    crashed = _ScriptedModel([_read_turn(1), _read_turn(2), RuntimeError("process died")])
    with pytest.raises(RuntimeError, match="process died"):
        run_plain_loop(ctx, crashed, "issue", max_turns=5)

    snapshot = load_loop_snapshot(ctx, stage="LOOP", round_no=0, task_id=ctx.task_id)
    assert snapshot is not None
    assert snapshot.turn_no == 2 and snapshot.tokens_spent == 200
    assert len(snapshot.messages) == 6  # system+user+2×(assistant,tool)

    resumed = _ScriptedModel([_finish_turn()])
    outcome = run_plain_loop(ctx, resumed, "issue", max_turns=5, resume_snapshot=snapshot)
    # (a) 从第 3 轮起跑:只发了一次请求就 finish,outcome.turns 记的是全局轮号
    assert outcome.turns == 3 and outcome.finish_declared
    # (b) 续跑的首次请求收到的正是崩溃前的完整历史(逐条同构,不是冷启动的两条)
    assert resumed.seen[0] == snapshot.messages
    assert resumed.seen[0][1]["content"] == "issue"
    assert sum(1 for m in resumed.seen[0] if m["role"] == "tool") == 2
    # (c) token 继续累加而不是重启
    assert outcome.tokens_used == 300
    assert outcome.tokens_prompt == 180 and outcome.tokens_completion == 120
    # 续跑本身必须留痕(from_turn = 快照轮号 + 1)
    resume_events = [e for e in ctx.tracker.events if e.tool == "loop_resume"]
    assert resume_events and resume_events[0].input["from_turn"] == 3


def test_resume_does_not_regrant_turn_ceiling(ctx: ToolContext) -> None:
    """上限不重授:剩 1 轮就只跑 1 轮;轮次已用满则一次请求都不发即超支。"""
    base = LoopSnapshot(
        stage="LOOP",
        round_no=0,
        turn_no=4,
        messages=[{"role": "user", "content": "issue"}],
        tokens_spent=400,
        tokens_prompt=240,
        tokens_completion=160,
        last_content="已读四轮",
        task_id=ctx.task_id,
    )
    almost = _ScriptedModel([_read_turn(5), _read_turn(6)])
    with pytest.raises(BudgetError) as info:
        run_plain_loop(ctx, almost, "issue", max_turns=5, resume_snapshot=base)
    assert len(almost.seen) == 1, "只剩 1 轮:不得把 max_turns 当新额度再跑"
    assert "max_turns" in str(info.value)
    # 已烧掉的量随异常带回(N-11 口径),不因恢复而蒸发
    assert info.value.tokens_spent == 500 and info.value.last_content == "第 5 轮读"

    spent = replace(base, turn_no=5)
    never = _ScriptedModel([_read_turn(6)])
    with pytest.raises(BudgetError, match="max_turns"):
        run_plain_loop(ctx, never, "issue", max_turns=5, resume_snapshot=spent)
    assert never.seen == [], "轮次已用满:恢复后必须立刻耗尽,而不是再要一次机会"


def test_finish_removes_snapshot_cancel_and_budget_keep_it(ctx: ToolContext) -> None:
    """完成的阶段不留可续快照;取消/超支的阶段必须留下连贯快照。"""
    snapshot_path = ctx.tracker.path.parent / SNAPSHOT_FILENAME
    run_plain_loop(ctx, FakeLLM(_read_script(2)), "issue", max_turns=5)
    assert not snapshot_path.exists(), "finish 后同一阶段绝不能再被续跑(否则轮次双计)"

    event = threading.Event()

    class _CancelAfterFirst(_ScriptedModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            turn = super().complete(messages, tools)
            event.set()  # 下一个 turn 边界被取消
            return turn

    cancelled = _CancelAfterFirst([_read_turn(1)])
    with pytest.raises(TaskCancelled):
        run_plain_loop(ctx, cancelled, "issue", max_turns=5, cancel_event=event, round_no=3)
    left = load_loop_snapshot(ctx, stage="LOOP", round_no=3, task_id=ctx.task_id)
    assert left is not None and left.turn_no == 1, "取消点是可续位置,快照必须留在盘上"

    exhausted = _ScriptedModel([_read_turn(i) for i in range(1, 6)])
    with pytest.raises(BudgetError):
        run_plain_loop(ctx, exhausted, "issue", max_turns=5, round_no=7)
    assert load_loop_snapshot(ctx, stage="LOOP", round_no=7, task_id=ctx.task_id) is not None


def _read_script(turns: int) -> list[dict]:
    return [
        {"tool": "read_file", "args": {"path": "src/dateparse.py", "offset": i}}
        for i in range(1, turns + 1)
    ] + [{"tool": "finish", "args": {"success": True, "summary": "查过了"}}]


def test_snapshot_write_crash_does_not_break_run(
    ctx: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.replace 抛错 = 写盘中途崩溃:循环照常跑完,且没有半截文件被信任。"""
    save_loop_snapshot(
        ctx,
        LoopSnapshot(
            stage="LOOP",
            turn_no=0,
            messages=[{"role": "user", "content": "旧快照"}],
            task_id=ctx.task_id,
        ),
    )
    original = (tmp_path / SNAPSHOT_FILENAME).read_text(encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("replace boom")

    monkeypatch.setattr(os, "replace", _boom)
    outcome = run_plain_loop(ctx, FakeLLM(_read_script(2)), "issue", max_turns=5)

    assert outcome.success and outcome.finish_declared, "快照机制不得把一次能跑完的运行搞失败"
    assert not (tmp_path / f"{SNAPSHOT_FILENAME}.tmp").exists()
    # finish 的删除同样没跑成(它 unlink 不依赖 replace),但读侧必须拿不到脏状态:
    # 要么文件已被删,要么仍是崩溃前那份完整旧快照——两种都比半截文件安全
    still = tmp_path / SNAPSHOT_FILENAME
    assert not still.exists() or still.read_text(encoding="utf-8") == original


def test_snapshot_disabled_writes_nothing_and_matches_messages(
    demo_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """loop_snapshot_enabled=false:零文件落地,且发给模型的消息序列逐条不变。"""
    runs: dict[str, tuple[list[list[dict[str, Any]]], Path]] = {}
    for enabled in (True, False):
        run_dir = tmp_path / ("on" if enabled else "off")
        run_dir.mkdir(parents=True)
        baseline = create_workspace(demo_repo, run_dir / "ws")
        ctx = ToolContext(
            task_id=f"T-{enabled}",
            workspace=run_dir / "ws",
            baseline_commit=baseline,
            tracker=Tracker(run_dir / "trajectory.jsonl", task_id=str(enabled)),
            report_dir=run_dir / "reports",
        )
        monkeypatch.setenv("PATCHPILOT_LOOP_SNAPSHOT_ENABLED", str(enabled).lower())
        get_settings.cache_clear()
        model = _ScriptedModel([_read_turn(1), _read_turn(2), RuntimeError("died")])
        with pytest.raises(RuntimeError):
            run_plain_loop(ctx, model, "issue", max_turns=5)
        runs[str(enabled)] = (model.seen, run_dir)

    assert (runs["True"][1] / SNAPSHOT_FILENAME).is_file()
    assert not (runs["False"][1] / SNAPSHOT_FILENAME).exists()
    assert json.dumps(runs["True"][0], sort_keys=True) == json.dumps(
        runs["False"][0], sort_keys=True
    ), "快照机制不得改变模型看到的东西"


# ---------- B 级:图 / 服务 ----------


def test_graph_resume_finishes_and_reruns_every_gate(tmp_path: Path) -> None:
    """崩溃在 PROPOSE 第 1 轮 → 续跑必须复位工作区、重打补丁、重跑门禁与双跑复核。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    task_id = "T-RESUME-GRAPH"
    run_dir = tmp_path / "runs" / task_id
    # localize 三轮收敛 + plan(非消耗请求)+ propose 打上崩溃残留后在第 2 轮死掉
    first_model = _CrashAfter(
        [
            *_localize_script(),
            {"tool": "apply_patch", "args": {"patch_text": block(_partial_diff())}},
        ],
        5,
    )
    with pytest.raises(_ProcessDied):
        run_task_graph(
            bug, first_model, runs_root=tmp_path / "runs", task_id=task_id, run_dir=run_dir
        )
    target = run_dir / "workspace" / "src" / "dateparse.py"
    assert MARKER in target.read_text(encoding="utf-8")
    snapshot = load_loop_snapshot(run_dir, stage="PROPOSE_PATCH", round_no=1, task_id=task_id)
    assert snapshot is not None and snapshot.turn_no == 1

    # 检查点里的每个通道都必须可序列化(M6 新增 baseline_commit 也在此列)
    from app.graph.checkpoint import make_sqlite_checkpointer

    reader = make_sqlite_checkpointer(run_dir / "checkpoints.sqlite")
    assert reader is not None
    tup = reader.get_tuple({"configurable": {"thread_id": task_id}})
    assert tup is not None
    channels = json.loads(json.dumps(tup.checkpoint["channel_values"], default=str))
    assert len(str(channels["baseline_commit"])) == 40
    reader.conn.close()

    second = run_task_graph(
        bug,
        FakeLLM(
            [
                {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
                {"tool": "run_tests", "args": {"test_set": "failed"}},
                {"tool": "run_tests", "args": {"test_set": "regression"}},
                {"tool": "finish", "args": {"success": True, "summary": "复位后按锚点重打"}},
            ]
        ),
        runs_root=tmp_path / "runs",
        task_id=task_id,
        run_dir=run_dir,
        resume=True,
    )
    assert second.status == "FINISHED" and second.verdict == "resolved"
    assert second.changed_files == ["src/dateparse.py"]
    assert MARKER not in target.read_text(encoding="utf-8"), "复位必须真的发生"

    events = _events(run_dir)
    names = [e["tool"] for e in events]
    assert names.count("create_workspace") == 1, "已完成阶段不得重跑(prepare 幂等)"
    start = names.index("resume_from_checkpoint")
    after = names[start:]
    # 门禁与双跑复核在**续跑的这次执行**里真跑过(不是复用崩溃前的判断)
    assert "resume_reset_workspace" in after
    reset_index = names.index("resume_reset_workspace")
    assert reset_index > start, "复位必须发生在续跑决策之后、补丁重打之前"
    assert events[reset_index]["output_summary"]["rolled_back"] is True
    assert sum(1 for name in after if name == "apply_gate") >= 1
    assert sum(1 for name in after if name == "verify") >= 1
    assert sum(1 for name in after if name == "verify_double_run") >= 1
    resume_event = events[start]
    assert resume_event["input"]["checkpoint_id"], "续跑必须记下它读的是哪一个检查点"
    assert resume_event["input"]["next_node"] == "propose"
    assert resume_event["output_summary"]["loop_snapshot_messages"] > 0
    loop_resume = [e for e in events[start:] if e["tool"] == "loop_resume"]
    assert loop_resume and loop_resume[0]["input"]["from_turn"] == 2
    # 累计量不双计:续跑报告的 token 不少于崩溃前已烧的量
    assert second.tokens_used >= snapshot.tokens_spent


def test_prepare_resume_resets_writable_stage_only(tmp_path: Path) -> None:
    """可写阶段复位到基线;只读阶段保留崩溃现场(复位会毁掉取证与已读的上下文)。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    workspace = tmp_path / "workspace"
    baseline = materialize_repo(bug.repo_dir, workspace, extra_commit=False)
    dirty = workspace / "src" / "dateparse.py"

    def _nodes(task_id: str) -> TaskNodes:
        return TaskNodes(
            bug=bug,
            model=FakeLLM([]),
            workspace=workspace,
            tracker=Tracker(tmp_path / f"{task_id}.jsonl", task_id=task_id),
            report_dir=tmp_path / "reports",
            max_rounds=2,
            max_turns=3,
        )

    text = dirty.read_text(encoding="utf-8")
    dirty.write_text(_mark_partial(text), encoding="utf-8", newline="\n")
    save_loop_snapshot(
        tmp_path,
        LoopSnapshot(
            stage="PROPOSE_PATCH",
            round_no=1,
            turn_no=2,
            messages=[{"role": "user", "content": "按计划继续"}],
            tokens_spent=300,
            task_id="T-WRITE",
        ),
    )
    state = {
        "bug_id": bug.id,
        "round_no": 1,
        "status": "PROPOSE_PATCH",
        "baseline_commit": baseline,
        "tokens_used": 900,
        # S02b:合法检查点必须携带持久化 deadline(恢复不重授时间预算)
        "deadline_epoch": time.time() + 900,
    }
    nodes = _nodes("T-WRITE")
    config = prepare_resume(
        nodes,
        _StubGraph(state, "propose"),
        run_dir=tmp_path,
        config={"recursion_limit": 12},
        task_id="T-WRITE",
    )
    assert config is not None
    # 主代理复核改的口径(原实现是 limit + step,等于给崩溃的任务多发循环额度):
    # 只顺延**剩余额度**——stub 的检查点 step=3,原 limit=12 ⇒ 续跑拿 9,
    # 整条 thread 的累计步数上界仍是那次正常运行的 12。崩溃不得换来更多的循环余地。
    assert config["recursion_limit"] == 9, config["recursion_limit"]
    assert nodes.ctx is not None and nodes.baseline_commit == baseline
    assert nodes.resume_snapshot is not None and nodes.resume_snapshot.turn_no == 2
    assert working_tree_is_clean(workspace) and MARKER not in dirty.read_text(encoding="utf-8")
    tools = [e.tool for e in nodes.tracker.events]
    assert "resume_reset_workspace" in tools and "resume_from_checkpoint" in tools

    # 只读阶段:不复位、不记复位事件,现场原样
    dirty.write_text(_mark_partial(text), encoding="utf-8", newline="\n")
    readonly = _nodes("T-READ")
    assert prepare_resume(
        readonly,
        _StubGraph(state, "localize"),
        run_dir=tmp_path,
        config={"recursion_limit": 12},
        task_id="T-READ",
    )
    assert MARKER in dirty.read_text(encoding="utf-8")
    assert "resume_reset_workspace" not in [e.tool for e in readonly.tracker.events]

    # 额度在崩溃前就已烧完 → 拒绝续跑,而且**一个字节都不动工作区**:
    # 拒绝之后调用方会退回既有终态口径,那时取证现场必须还是崩溃时的样子。
    # (复核时这正是原实现的顺序缺陷:先复位再判定,拒绝的决定已经把现场抹掉了。)
    exhausted = _nodes("T-EXH")
    dirty.write_text(_mark_partial(text), encoding="utf-8", newline="\n")
    assert (
        prepare_resume(
            exhausted,
            _StubGraph(state, "propose", step=12),
            run_dir=tmp_path,
            config={"recursion_limit": 12},
            task_id="T-EXH",
        )
        is None
    )
    assert "resume_unavailable" in [e.tool for e in exhausted.tracker.events]
    assert "resume_reset_workspace" not in [e.tool for e in exhausted.tracker.events]
    assert MARKER in dirty.read_text(encoding="utf-8"), "拒绝续跑不得先毁掉崩溃现场"


_SERVICES: list[TaskService] = []


@pytest.fixture(autouse=True)
def _drain_services() -> Generator[None, None, None]:
    """每个用例结束时把本文件创建的 service 收干。

    为什么必须:recover_stale 的续跑路径会把任务 submit 到线程池,用例断言完"已重新入队"
    就返回的话,**续跑的那次真实图执行会带着 FakeLLM 和物化工作区一直在后台跑**。
    实测过代价:全量套件里它和后续用例抢 `get_settings()` 缓存(conftest 记过同一类窗口),
    把 tests/test_auth.py 的鉴权用例打成一个只在整套顺序下才复现的假失败。
    生产语义(服务退出时才排空)不在本用例范围内,这里要的是"用例之间不留活口"。
    """
    yield
    while _SERVICES:
        service = _SERVICES.pop()
        service.shutdown()


def _service(tmp_path: Path) -> TaskService:
    service = TaskService(
        repo=Repository(tmp_path / "db.sqlite3"),
        runs_root=tmp_path / "runs",
        bugs_root=BUG_ROOT,
    )
    _SERVICES.append(service)
    return service


def _zombie(service: TaskService, tmp_path: Path, *, with_snapshot: bool) -> tuple[str, str, Path]:
    """造一行崩溃残留的 RUNNING 任务(graph/fake/BUG-001),按需附带循环快照。"""
    task_id = "T-ZOMBIE"
    idem = hashlib.sha256(b"BUG-001|graph|fake").hexdigest()
    run_dir = tmp_path / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_snapshot:
        save_loop_snapshot(
            run_dir,
            LoopSnapshot(
                stage="PROPOSE_PATCH",
                round_no=1,
                turn_no=2,
                messages=[{"role": "user", "content": "继续"}],
                tokens_spent=200,
                task_id=task_id,
            ),
        )
    service.repo.create_task(
        task_id=task_id,
        idem_key=idem,
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="x",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(run_dir),
    )
    service.repo.set_status_unless_terminal(task_id, "RUNNING")
    return task_id, idem, run_dir


def _stub_engine(seen: list[dict[str, Any]]) -> Any:
    """替换 run_task_graph:记录入参并直接收敛 FINISHED(服务侧只测"是否续跑")。"""

    def _run(bug: Any, model: Any, **kwargs: Any) -> TaskResult:
        seen.append(kwargs)
        return TaskResult(
            task_id=str(kwargs.get("task_id")),
            bug_id=bug.id,
            status="FINISHED",
            verdict="resolved",
            outcome="resolved",
            engine="graph",
            run_dir=str(kwargs.get("run_dir")),
        )

    return _run


def _await_terminal(service: TaskService, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        row = service.repo.get_task(task_id)
        assert row is not None
        if row["status"] in TERMINAL_STATUSES:
            return row
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached a terminal status")


def test_recover_stale_requeues_zombie_with_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    task_id, _idem, run_dir = _zombie(service, tmp_path, with_snapshot=True)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("app.graph.runner.run_task_graph", _stub_engine(seen))

    assert service.recover_stale() == 1
    row = _await_terminal(service, task_id)
    assert seen and seen[0]["resume"] is True, "有快照必须走续跑入口"
    assert seen[0]["task_id"] == task_id and Path(seen[0]["run_dir"]) == run_dir
    assert row["status"] == "FINISHED", "僵尸任务被续跑到了结构化终态,而不是判死"
    service.shutdown()


def test_recover_stale_without_snapshot_is_byte_identical_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无快照 = 旧行为原样:判 NEEDS_REVIEW、清残留锁、绝不入队(既有测试同样钉这条)。"""
    service = _service(tmp_path)
    task_id, idem, _run_dir = _zombie(service, tmp_path, with_snapshot=False)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("app.graph.runner.run_task_graph", _stub_engine(seen))
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=600)  # 崩溃残留的锁

    assert service.recover_stale() == 1
    assert service.repo.get_task(task_id)["status"] == "NEEDS_REVIEW"
    assert seen == []
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=5), "旧锁必须被 force_release"
    service.lock.release(f"task:{idem}")
    service.shutdown()


def test_resume_disabled_by_settings_keeps_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATCHPILOT_RESUME_ON_RESTART", "false")
    get_settings.cache_clear()
    service = _service(tmp_path)
    task_id, _idem, _run_dir = _zombie(service, tmp_path, with_snapshot=True)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("app.graph.runner.run_task_graph", _stub_engine(seen))

    assert service.recover_stale() == 1
    assert seen == [] and service.repo.get_task(task_id)["status"] == "NEEDS_REVIEW"
    service.shutdown()


def test_concurrent_resume_is_blocked_by_existing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """双恢复拦截用的是既有任务锁:锁被另一恢复方持有 → 不入队,也不强拆他人的锁。"""
    service = _service(tmp_path)
    task_id, idem, _run_dir = _zombie(service, tmp_path, with_snapshot=True)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("app.graph.runner.run_task_graph", _stub_engine(seen))
    assert service.lock.acquire(f"task:{idem}", ttl_seconds=600)

    assert service.recover_stale() == 1
    assert seen == [], "同一任务不得并行跑两份恢复"
    assert service.repo.get_task(task_id)["status"] == "NEEDS_REVIEW"
    assert not service.lock.acquire(f"task:{idem}", ttl_seconds=5), "他人锁不得被强清"
    service.lock.release(f"task:{idem}")
    service.shutdown()
