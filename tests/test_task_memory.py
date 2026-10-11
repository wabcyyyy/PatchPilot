"""G06:任务记忆、会话配对不变量与阶段上下文装配。

这层的东西只有一种证明方式:**看实际装配出来的请求文本**。
所以本文件既测纯对象(render/model_update/repair_orphans),也测接线
(registry 自动登记观测、run_plain_loop 的孤儿补齐与快照绑定);
T10 那条端到端断言的是"PLAN/PROPOSE 那一次请求里真的带着定位阶段的观测",
不是"内部 memory 字段有值"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent.session import ORPHAN_REPLY, SESSION_PROTOCOL, SessionState
from app.context.assembler import ContextAssembler
from app.context.task_memory import (
    MEMORY_SCHEMA_VERSION,
    TaskMemory,
    load_memory_state,
    save_memory_state,
)
from app.errors import BudgetError
from app.tools.base import ToolContext
from app.tools.registry import execute
from app.tools.tracker import Tracker

TASK = "T-MEM"


def _memory() -> TaskMemory:
    memory = TaskMemory(TASK, "s-test")
    memory.set_context(
        goal="空字符串输入应返回 None",
        constraints=["禁止修改测试文件"],
        acceptance=["tests/test_x.py::test_empty"],
    )
    return memory


def _ctx(tmp_path: Path, **overrides: Any) -> ToolContext:
    return ToolContext(
        task_id=TASK,
        workspace=tmp_path / "ws",
        baseline_commit="",
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id=TASK),
        report_dir=tmp_path / "report",
        test_sets={},
        allowed_paths=None,
        **overrides,
    )


def _obs(memory: TaskMemory, count: int = 3) -> list[str]:
    refs = []
    for index in range(count):
        entry = memory.record_evidence(
            source="read_file",
            stage="LOCALIZE",
            round_no=0,
            detail=f"read_file src/mod{index}.py 取 400 行",
            path=f"src/mod{index}.py",
            line_range="1-400",
        )
        refs.append(entry.ref)
    return refs


# ---------- 记忆:观测、假设、失效 ----------


def test_empty_memory_renders_nothing() -> None:
    """还没有任何观测/假设/计划时**不出块**:只有约束与验收清单不算内容。

    这条守的是"接入记忆后,任务的第一个阶段与接入前逐字节相同"——否则每个既有断言
    都要为重排的 system 让路,而那是伪装成重构的行为变更。
    """
    memory = _memory()
    assert memory.render(max_chars=3000) == ""
    memory.record_evidence(
        source="list_files", stage="LOCALIZE", round_no=0, detail="列出 12 个文件"
    )
    assert "### 工作记忆" in memory.render(max_chars=3000)


def test_observations_are_platform_authored_and_versioned() -> None:
    memory = _memory()
    before = memory.version
    _obs(memory, 2)
    assert memory.version > before
    assert [e.ref for e in memory.evidence] == ["E1", "E2"]
    assert memory.state_hash() != _memory().state_hash()


def test_model_cannot_write_platform_statuses() -> None:
    """`verified/confirmed` 一律点名拒绝,并明确告诉模型为什么(SPEC:推测不得写成已验证事实)。"""
    memory = _memory()
    refs = _obs(memory)
    applied, rejected = memory.model_update(
        {"hypotheses": [{"text": "已经修好了", "status": "verified", "refs": refs}]}
    )
    assert applied == [] and len(rejected) == 1
    assert "platform-only" in rejected[0]
    assert memory.hypotheses == []


def test_hypothesis_needs_real_references() -> None:
    memory = _memory()
    _obs(memory)
    applied, rejected = memory.model_update(
        {
            "hypotheses": [
                {"text": "int() 前没 strip", "status": "supported", "refs": ["E99"]},
                {"text": "可能是编码问题", "status": "hypothesis", "refs": []},
                {"text": "断言约束了返回类型", "status": "supported", "refs": ["E1"]},
            ]
        }
    )
    assert applied == ["H1", "H2"]
    assert any("不存在的证据" in reason for reason in rejected)
    assert {h.hid: h.status for h in memory.hypotheses} == {"H1": "hypothesis", "H2": "supported"}
    assert memory.hypotheses[1].support == ["E1"]


def test_supported_without_evidence_is_rejected() -> None:
    memory = _memory()
    applied, rejected = memory.model_update(
        {"hypotheses": [{"text": "我确信是这里", "status": "supported", "refs": []}]}
    )
    assert applied == [] and "必须带上支撑/反例的证据引用" in rejected[0]


def test_refuted_status_lands_in_counter_refs() -> None:
    memory = _memory()
    _obs(memory)
    applied, _ = memory.model_update(
        {"hypotheses": [{"text": "不是并发问题", "status": "refuted", "refs": ["E2"]}]}
    )
    hypothesis = memory.hypotheses[0]
    assert applied and hypothesis.counter == ["E2"] and hypothesis.support == []


def test_exclusion_requires_reason() -> None:
    memory = _memory()
    applied, rejected = memory.model_update({"excluded": [{"text": "改测试期望"}]})
    assert applied == [] and "text 与 reason" in rejected[0]
    applied, _ = memory.model_update({"excluded": [{"text": "改测试期望", "reason": "门禁禁止"}]})
    assert applied and memory.excluded[0].reason == "门禁禁止"


def test_stale_marking_only_touches_older_revisions() -> None:
    """写之前的观测失效,写之后新增的不失效(G05 的 workspace_revision 同一条线)。"""
    memory = _memory()
    old = memory.record_evidence(
        source="read_file", stage="LOCALIZE", round_no=0, detail="改前读的", workspace_revision=0
    )
    marked = memory.mark_stale(from_revision=1)
    assert marked == 1 and old.stale is True
    fresh = memory.record_evidence(
        source="read_file",
        stage="PROPOSE_PATCH",
        round_no=1,
        detail="改后读的",
        workspace_revision=1,
    )
    assert memory.mark_stale(from_revision=2) == 1
    assert fresh.stale is True and old.stale is True


def test_render_separates_facts_from_model_claims() -> None:
    memory = _memory()
    refs = _obs(memory)
    memory.record_evidence(
        source="platform.verify",
        stage="VERIFY",
        round_no=1,
        detail="重跑",
        verdict="failed_ok=False",
    )
    memory.model_update(
        {"hypotheses": [{"text": "边界少了一个分支", "status": "supported", "refs": refs[:1]}]}
    )
    block = memory.render(max_chars=4000, current_revision=1)
    assert "平台观测(最近在前)" in block and "模型假设(未经平台验证)" in block
    assert "平台判定" in block and "failed_ok=False" in block
    assert "read_evidence" not in block  # 没有原文引用就不许提取回
    assert "已失效" in block and "工作区已写入 1 代" in block


def test_render_reports_how_much_it_dropped() -> None:
    """裁掉必须报数:一个"看起来完整"的记忆块比明说"少 40 条"更危险。"""
    memory = _memory()
    _obs(memory, 45)
    block = memory.render(max_chars=600)
    assert len(block) <= 600
    assert "另有" in block and "条未显示" in block
    assert "[E45]" in block  # 最新一条必须在场(倒序渲染:越近越有用)
    assert "[E1]" not in block  # 被裁掉的确实是尾部老条目,不是随机丢


def test_set_context_is_idempotent_for_the_same_frozen_input() -> None:
    """恢复路径会重放同一次冻结:内容没变就**不能**升版本。

    升了版本,记忆 hash 与崩溃前那份不同 ⇒ 绑定校验把自己的恢复判成 mismatch,
    任务被错误收敛成待审(这是本轮全量里真实红过的两条 resume 用例的根因)。
    """
    memory = _memory()
    _obs(memory)
    before_version, before_hash = memory.version, memory.state_hash()
    memory.set_context(
        goal="空字符串输入应返回 None",
        constraints=["禁止修改测试文件"],
        acceptance=["tests/test_x.py::test_empty"],
    )
    assert (memory.version, memory.state_hash()) == (before_version, before_hash)
    memory.set_context(constraints=["禁止修改测试文件", "新增一条硬约束"])
    assert memory.version == before_version + 1 and memory.state_hash() != before_hash


def test_memory_state_round_trip(tmp_path: Path) -> None:
    memory = _memory()
    refs = _obs(memory)
    memory.model_update({"hypotheses": [{"text": "假设", "status": "hypothesis", "refs": []}]})
    save_memory_state(tmp_path, memory)
    restored = load_memory_state(tmp_path)
    assert restored is not None and restored.state_hash() == memory.state_hash()
    assert restored.version == memory.version and [e.ref for e in restored.evidence] == refs
    # 恢复后模型引用旧 ref 仍要能被接受(引用集合是按条目重建的)
    applied, rejected = restored.model_update(
        {"hypotheses": [{"text": "补一个有依据的假设", "status": "supported", "refs": [refs[0]]}]}
    )
    assert applied and not rejected


def test_unknown_schema_version_is_ignored(tmp_path: Path) -> None:
    """版本不符就当没有:猜着迁移等于伪造记忆内容。"""
    (tmp_path / "memory_state.json").write_text(
        json.dumps({"schema_version": MEMORY_SCHEMA_VERSION + 7, "task_id": TASK}), encoding="utf-8"
    )
    assert load_memory_state(tmp_path) is None


def test_missing_memory_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_memory_state(tmp_path) is None


# ---------- 会话:配对不变量与身份 ----------


def _assistant(call_ids: list[str]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def test_orphan_detection_and_repair() -> None:
    messages = [
        {"role": "system", "content": "s"},
        _assistant(["c1", "c2"]),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert SessionState.orphan_tool_calls(messages) == ["c2"]
    repaired, added = SessionState.repair_orphans(messages)
    assert added == 1
    # 补齐紧跟在声明它的那条 assistant 之后:端点要求配对相邻,不接受"最后一行"式的兜底
    assert repaired[2] == {"role": "tool", "tool_call_id": "c2", "content": ORPHAN_REPLY}
    assert SessionState.orphan_tool_calls(repaired) == []
    again, added_again = SessionState.repair_orphans(repaired)
    assert (added_again, again) == (0, repaired)  # 幂等,且不改动已配对的消息


def test_repair_does_not_invent_success() -> None:
    """占位回执写的是"没有记录到结果",不是"成功"。"""
    repaired, added = SessionState.repair_orphans([_assistant(["c9"])])
    assert added == 1
    assert "no recorded result" in repaired[1]["content"]
    assert "success" not in repaired[1]["content"]


def test_session_identity_moves_with_capability_set() -> None:
    session = SessionState("T1")
    session.begin_stage(
        "LOCALIZE", round_no=0, allowed_tools=["read_file", "finish"], memory_version=1
    )
    first = session.identity_hash()
    session.begin_stage("PLAN", round_no=0, allowed_tools=["finish"], memory_version=4)
    assert session.identity_hash() != first
    assert session.current_stage == "PLAN"
    restored = SessionState.from_snapshot(session.snapshot())
    assert restored is not None and restored.identity_hash() == session.identity_hash()
    assert SessionState.from_snapshot({"protocol_version": "other"}) is None
    assert session.protocol == SESSION_PROTOCOL


def test_session_tracks_calls_and_answers() -> None:
    session = SessionState("T1")
    session.declare_call("c1", "read_file", stage="LOCALIZE", round_no=0, turn_no=1)
    assert [c.call_id for c in session.unanswered()] == ["c1"]
    session.answer_call("c1")
    assert session.unanswered() == []


# ---------- 装配器:同一阶段不叠加 ----------


def test_assembly_is_stable_within_a_stage() -> None:
    """同一 (阶段, 轮次) 二次装配复用第一次的成品 ⇒ 模型的 system 里同一份说明只出现一次。"""
    memory = _memory()
    _obs(memory)
    assembler = ContextAssembler()
    first = assembler.assemble(
        stage="PLAN",
        round_no=0,
        session=SessionState("T1"),
        memory=memory,
        skeleton="<repo_skeleton>abc</repo_skeleton>",
        max_chars=3000,
        allowed_tools=["finish"],
    )
    _obs(memory, 5)  # 记忆在别的阶段继续增长,不能把已装配的 PLAN 上下文改写掉
    second = assembler.assemble(
        stage="PLAN",
        round_no=0,
        session=None,
        memory=memory,
        skeleton="<repo_skeleton>abc</repo_skeleton>",
        max_chars=3000,
    )
    assert second.reused is True and second.text == first.text
    assert first.text.count("### 工作记忆") == 1
    assert first.text.count("<repo_skeleton>") == 1
    assert first.text.startswith("<repo_skeleton>")  # 骨架仍在最前(与 M2 的拼接顺序一致)


def test_assembly_reflects_new_memory_on_next_round() -> None:
    memory = _memory()
    assembler = ContextAssembler()
    first = assembler.assemble(
        stage="PROPOSE_PATCH", round_no=0, session=None, memory=memory, max_chars=3000
    )
    assert first.text == ""  # 没有观测就没有块:与接入前逐字节相同
    _obs(memory)
    second = assembler.assemble(
        stage="PROPOSE_PATCH", round_no=1, session=None, memory=memory, max_chars=3000
    )
    assert "### 工作记忆" in second.text and second.memory_chars > 0


def test_assembler_invalidate_forces_fresh_assembly() -> None:
    memory = _memory()
    assembler = ContextAssembler()
    a = assembler.assemble(
        stage="LOCALIZE", round_no=1, session=None, memory=memory, max_chars=3000
    )
    assembler.invalidate("LOCALIZE", from_round=1)
    b = assembler.assemble(
        stage="LOCALIZE", round_no=1, session=None, memory=memory, max_chars=3000
    )
    assert a is not b


# ---------- 接线:工具层自动登记、模型提交通道、失效标记 ----------


def _guard_patch() -> str:
    """demo_repo 的"正确修复"unified diff(与 test_tools 同法从真仓库取,手写 hunk 会漂)。"""
    import shutil
    import tempfile

    from app.gitops.differ import working_tree_diff
    from app.gitops.snapshot import create_workspace
    from app.gitops.testing import materialize_repo

    tmp = Path(tempfile.mkdtemp(prefix="memdiff-"))
    try:
        repo_src = tmp / "src"
        materialize_repo(Path(__file__).parent / "fixtures" / "demo_repo", repo_src)
        create_workspace(repo_src, tmp / "ws")
        target = tmp / "ws" / "src" / "dateparse.py"
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "    if value is None:\n        return None\n",
                "    if value is None:\n        return None\n"
                "    if not value.strip():\n        return None\n",
            ),
            encoding="utf-8",
            newline="\n",
        )
        return working_tree_diff(tmp / "ws").diff_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_execute_records_observations_without_model_help(tmp_path: Path) -> None:
    """模型一个字都没写,记忆里已经有了"读过哪个文件、哪些行、全文多少行"。"""
    memory = _memory()
    ctx = _ctx(tmp_path, memory=memory, session=SessionState(TASK, memory.session_id))
    (tmp_path / "ws" / "src").mkdir(parents=True)
    (tmp_path / "ws" / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    result = execute(ctx, "read_file", {"path": "src/a.py"})
    assert result.ok
    assert [entry.ref for entry in memory.evidence] == ["E1"]
    entry = memory.evidence[0]
    assert entry.source == "read_file" and entry.path == "src/a.py" and entry.line_range == "1-2"
    assert "全文 2 行" in entry.detail


def test_failed_receipt_is_an_observation_too(tmp_path: Path) -> None:
    """走死的口子是一条事实:它比成功更能阻止下一阶段重复同一次调查。"""
    memory = _memory()
    ctx = _ctx(tmp_path, memory=memory)
    result = execute(ctx, "read_file", {"path": "nope.py"})
    assert not result.ok
    assert "失败" in memory.evidence[0].detail and "nope.py" in memory.evidence[0].path


def test_write_marks_memory_observations_stale(demo_repo: Path, tmp_path: Path) -> None:
    from app.gitops.snapshot import create_workspace
    from tests.conftest import block

    workspace = tmp_path / "ws"
    create_workspace(demo_repo, workspace)
    memory = _memory()
    ctx = _ctx(tmp_path, memory=memory)
    ctx.workspace = workspace
    execute(ctx, "read_file", {"path": "src/dateparse.py"})
    assert ctx.workspace_revision == 0 and memory.evidence[0].stale is False
    patch = block(_guard_patch())
    result = execute(ctx, "apply_patch", {"patch_text": patch})
    assert result.ok, result.error
    assert ctx.workspace_revision == 1
    assert memory.evidence[0].stale is True
    written = [entry for entry in memory.evidence if entry.source == "apply_patch"]
    assert written and written[0].workspace_revision == 1 and written[0].stale is False


def test_update_memory_tool_reports_rejections(tmp_path: Path) -> None:
    memory = _memory()
    refs = _obs(memory)
    ctx = _ctx(tmp_path, memory=memory)
    result = execute(
        ctx,
        "update_memory",
        {
            "hypotheses": [
                {"text": "空串走了 int()", "status": "supported", "refs": [refs[0]]},
                {"text": "已经修好", "status": "verified", "refs": []},
            ]
        },
        state="LOCALIZE",
        round_no=0,
    )
    assert result.ok
    output = result.output
    assert output["accepted"] and len(output["rejected"]) == 1
    assert "platform-only" in output["rejected"][0]
    assert output["memory_version"] == memory.version
    assert memory.hypotheses[0].support == [refs[0]]
    # 假设登记本身不再产生一条"观测"(它有自己的区)
    assert [entry.source for entry in memory.evidence] == ["read_file", "read_file", "read_file"]


def test_update_memory_is_refused_without_memory(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = execute(ctx, "update_memory", {"hypotheses": [{"text": "x"}]})
    assert not result.ok and "not available" in (result.error or "")


def test_unwired_context_records_nothing(tmp_path: Path) -> None:
    """没接记忆的调用完全不留痕:记忆是可选装配,不是每条运行的必经副作用。"""
    ctx = _ctx(tmp_path)
    (tmp_path / "ws").mkdir(parents=True)
    (tmp_path / "ws" / "a.py").write_text("x = 1\n", encoding="utf-8")
    execute(ctx, "read_file", {"path": "a.py"})
    assert ctx.memory is None and load_memory_state(tmp_path) is None


# ---------- 循环层:实际请求里带着证据、孤儿补齐、快照绑定 ----------


class _ScriptedModel:
    """按脚本发回合,并把**每次实际发出去的 messages** 留下来(契约要在真实请求上验)。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        from app.llm.base import AssistantTurn, ToolCall

        self.seen.append([dict(message) for message in messages])
        if self.script:
            step = self.script.pop(0)
            if step.get("text") is not None:
                return AssistantTurn(content=step["text"], finish_reason="stop")
            return AssistantTurn(
                content=step.get("content", ""),
                tool_calls=[
                    ToolCall(
                        id=f"c{len(self.seen)}", name=step["tool"], arguments=step.get("args", {})
                    )
                ],
                finish_reason="tool_calls",
            )
        return AssistantTurn(
            content="收",
            tool_calls=[
                ToolCall(id="cf", name="finish", arguments={"success": False, "summary": "end"})
            ],
            finish_reason="tool_calls",
        )


def _loop_ctx(demo_repo: Path, tmp_path: Path, **overrides: Any) -> ToolContext:
    from app.gitops.snapshot import create_workspace

    workspace = tmp_path / "ws"
    baseline = create_workspace(demo_repo, workspace)
    ctx = _ctx(tmp_path, **overrides)
    ctx.workspace = workspace
    ctx.baseline_commit = baseline
    return ctx


def test_plan_request_carries_localize_observations(demo_repo: Path, tmp_path: Path) -> None:
    """T10 的退出判据:PLAN 那一次请求里真的带着定位阶段的观测(含行范围与取回引用)。

    断言对象是**模型实际收到的消息**,不是 memory 的内部字段——SPEC 明说不能只断言字段有值。
    """
    from app.graph.plain_loop import run_plain_loop

    ctx = _loop_ctx(demo_repo, tmp_path, memory=_memory())
    ctx.session = SessionState(TASK, ctx.memory.session_id)  # type: ignore[union-attr]
    (ctx.workspace / "src" / "huge.py").write_text(
        "\n".join(f"needle_{index} = {'z' * 70}" for index in range(400)), encoding="utf-8"
    )
    memory, session, assembler = ctx.memory, ctx.session, ContextAssembler()  # type: ignore[assignment]

    localize_model = _ScriptedModel(
        [
            {"tool": "read_file", "args": {"path": "src/huge.py", "limit": 400}},
            {"tool": "finish", "args": {"success": True, "summary": "根因在 needle 的解析分支"}},
        ]
    )
    run_plain_loop(
        ctx,
        localize_model,
        "定位阶段提示",
        max_turns=4,
        state_label="LOCALIZE",
        allowed_tools=["read_file", "read_evidence", "update_memory", "finish"],
        extra_system=assembler.assemble(
            stage="LOCALIZE", round_no=0, session=session, memory=memory, max_chars=3000
        ).text,
    )
    assert memory.evidence, "定位阶段没有登记任何观测"

    plan_context = assembler.assemble(
        stage="PLAN",
        round_no=0,
        session=session,
        memory=memory,
        max_chars=3000,
        current_revision=ctx.workspace_revision,
        allowed_tools=["finish"],
    )
    plan_model = _ScriptedModel([{"text": "只改 needle 分支的入口,加一次 strip。"}])
    outcome = run_plain_loop(
        ctx,
        plan_model,
        "计划阶段提示",
        max_turns=3,
        state_label="PLAN",
        allowed_tools=["finish"],
        complete_on_text=True,
        extra_system=plan_context.text,
    )
    assert outcome.completion == "text"
    system = str(plan_model.seen[0][0]["content"])
    assert "### 工作记忆" in system
    assert "[E1] LOCALIZE r0 read_file" in system and "行 1-" in system
    # 被裁掉的原文在引用里同时带了取回方式:G05 与 G06 在这一条上接上了
    assert "read_evidence(" in system
    assert plan_context.memory_chars > 0 and plan_context.reused is False


def test_plain_loop_repairs_orphan_tool_call_before_request(
    demo_repo: Path, tmp_path: Path
) -> None:
    """崩溃切在"assistant 已声明、回执未落"之间 ⇒ 续跑前必须补齐,不能把孤儿发出去。"""
    from app.graph.loop_state import LoopSnapshot
    from app.graph.plain_loop import run_plain_loop

    ctx = _loop_ctx(demo_repo, tmp_path, memory=_memory(), session=SessionState(TASK))
    snapshot = LoopSnapshot(
        stage="LOOP",
        round_no=0,
        turn_no=1,
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "issue"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "orphan-1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"},
                    }
                ],
            },
        ],
        tokens_spent=10,
        task_id=TASK,
    )
    model = _ScriptedModel([{"text": "接着想"}])
    # 这一轮的回合数刚好够它收尾;用例要的是"那一次请求发出去之前,孤儿已经被补齐",
    # 所以不需要让它真的把预算跑爆(跑爆与否由 test_f1_budget 一族负责)
    run_plain_loop(ctx, model, "issue", max_turns=3, resume_snapshot=snapshot)
    first = model.seen[0]
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "orphan-1"
        for message in first
    ), "孤儿 tool_call 被原样发给了端点"
    events = [
        json.loads(line)
        for line in ctx.tracker.path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    ]
    assert any(event["tool"] == "message_pairing_repaired" for event in events)


def test_snapshot_binds_session_and_memory(demo_repo: Path, tmp_path: Path) -> None:
    """快照带着会话与记忆版本:恢复方据此判断"还是不是崩溃前那一条会话"。"""
    from app.graph.loop_state import load_loop_snapshot
    from app.graph.plain_loop import run_plain_loop

    memory = _memory()
    ctx = _loop_ctx(
        demo_repo, tmp_path, memory=memory, session=SessionState(TASK, memory.session_id)
    )
    model = _ScriptedModel([{"tool": "list_files", "args": {}}, {"text": "还要再看一眼"}])
    with pytest.raises(BudgetError):
        # 回合用尽而阶段没收尾 ⇒ 快照必须留在 turn 边界上(崩溃恢复接的就是这一处)
        run_plain_loop(ctx, model, "issue", max_turns=2, round_no=0, state_label="LOOP")
    snapshot = load_loop_snapshot(ctx, stage="LOOP", round_no=0)
    assert snapshot is not None
    assert snapshot.session_id == ctx.session.session_id  # type: ignore[union-attr]
    assert snapshot.session_hash == ctx.session.identity_hash()  # type: ignore[union-attr]
    assert snapshot.memory_hash == memory.state_hash()
    assert snapshot.memory_version == memory.version
    # 记忆本体在同一个切点落盘:两份必须来自同一个边界,否则续跑带的是错一版的证据
    restored = load_memory_state(tmp_path)
    assert restored is not None and restored.state_hash() == memory.state_hash()


def test_binding_mismatch_predicate_only_fires_when_both_bound() -> None:
    from app.graph.loop_state import LoopSnapshot
    from app.graph.resume import binding_mismatch

    session = SessionState(TASK, "s-1")
    memory = TaskMemory(TASK, "s-1")
    bound = LoopSnapshot(
        stage="LOOP",
        session_id="s-1",
        session_hash=session.identity_hash(),
        memory_hash="0" * 64,
    )
    mismatched, readings = binding_mismatch(bound, session, memory)
    assert mismatched == ["memory_hash"]
    assert readings["snapshot"]["session_id"] == "s-1"
    # 旧快照(没有绑定)与未接记忆的运行都走既有语义:冷启动该阶段,而不是凭空判待审
    assert binding_mismatch(LoopSnapshot(stage="LOOP"), session, memory) == ([], {})
    assert binding_mismatch(None, session, memory) == ([], {})
