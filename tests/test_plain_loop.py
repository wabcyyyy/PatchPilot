"""M3 纯 Python 工具循环测试:FakeLLM 驱动的完整修复闭环。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.context.token_window import REASONING_STUB, STUB_PREFIX
from app.errors import BudgetError
from app.gitops.differ import working_tree_diff
from app.gitops.snapshot import create_workspace
from app.graph.plain_loop import run_plain_loop
from app.llm.base import AssistantTurn, messages_tokens
from app.llm.fake import FakeLLM
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext
from app.tools.tracker import Tracker
from tests.conftest import block

sys_path = Path(__file__).parent
FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]


@pytest.fixture()
def ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="T-LOOP")
    return ToolContext(
        task_id="T-LOOP",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=tracker,
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": REGRESSION_IDS},
    )


def _fix_diff() -> str:
    """在临时副本上生成 demo_repo 的正确修复 diff,喂给 FakeLLM 脚本。"""
    import shutil
    import tempfile

    from app.gitops.testing import materialize_repo

    tmp = Path(tempfile.mkdtemp(prefix="fixdiff-"))
    try:
        repo_src = tmp / "src"
        materialize_repo(sys_path / "fixtures" / "demo_repo", repo_src)
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


def _repair_script() -> list[dict]:
    return [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "空字符串未防御,已补早退分支"}},
    ]


def test_full_repair_loop_via_fake_llm(ctx: ToolContext) -> None:
    model = FakeLLM(_repair_script())
    outcome = run_plain_loop(ctx, model, "空字符串抛异常,应返回 None")

    assert outcome.success and outcome.finish_declared and outcome.patch_applied
    assert outcome.turns == len(_repair_script())

    # 工作区确实被修复,且全量测试通过
    target = ctx.workspace / "src" / "dateparse.py"
    assert "value.strip()" in target.read_text(encoding="utf-8")

    # 轨迹完整可解析
    assert ctx.tracker.path is not None
    events = [
        json.loads(line) for line in ctx.tracker.path.read_text(encoding="utf-8").splitlines()
    ]
    # P1-b:每轮模型输出先有一条 llm 事件(Thought),工具事件随后(Action/Observation)
    llm_events = [e for e in events if e["tool"] == "llm"]
    assert len(llm_events) == outcome.turns
    assert llm_events[0]["state"] == "LOOP"
    tools_used = [e["tool"] for e in events if e["tool"] != "llm"]
    assert tools_used[0] == "search_code" and tools_used[-1] == "finish"
    assert events[-1]["output_summary"]["patch_applied"] is True


def test_loop_finishes_false_when_script_exhausted(ctx: ToolContext) -> None:
    outcome = run_plain_loop(ctx, FakeLLM([{"tool": "list_files", "args": {}}]), "任意 issue")
    assert not outcome.success
    assert "exhausted" in outcome.summary


def test_loop_raises_budget_error_on_endless_content(ctx: ToolContext) -> None:
    class ChattyModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="思考中……", finish_reason="stop")

    with pytest.raises(BudgetError):
        run_plain_loop(ctx, ChattyModel(), "issue", max_turns=3)


def test_loop_raises_budget_error_when_tokens_exhausted(ctx: ToolContext) -> None:
    """累计 token 超预算 → BudgetError,且先于 max_turns 触发。"""

    class SpendthriftModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="x", finish_reason="stop", usage_tokens=100_000)

    with pytest.raises(BudgetError) as exc_info:
        run_plain_loop(ctx, SpendthriftModel(), "issue", max_turns=10, token_budget=50_000)
    assert "exceed budget" in str(exc_info.value)


def test_loop_token_budget_zero_disables_token_check(ctx: ToolContext) -> None:
    """token_budget=0 表示不限制:大用量模型也要到 max_turns 才耗尽。"""

    class SpendthriftModel:
        def complete(self, messages, tools):
            return AssistantTurn(content="x", finish_reason="stop", usage_tokens=100_000)

    with pytest.raises(BudgetError) as exc_info:
        run_plain_loop(ctx, SpendthriftModel(), "issue", max_turns=3, token_budget=0)
    assert "max_turns" in str(exc_info.value)


def test_loop_accumulates_prompt_completion_tokens(ctx: ToolContext) -> None:
    """N2a:循环累计 prompt/completion 明细;fake 下 prompt=0、completion=总量。"""
    model = FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "s"}}])
    outcome = run_plain_loop(ctx, model, "issue")
    assert outcome.tokens_prompt == 0
    assert outcome.tokens_completion > 0
    assert outcome.tokens_used == outcome.tokens_completion


def test_assistant_tool_calls_use_openai_wire_format(ctx: ToolContext) -> None:
    """回传给模型的 assistant 消息必须是 OpenAI 标准格式(type/function/arguments 为 JSON 字符串),
    否则严格的服务端(如 DeepSeek)会 400。"""
    captured: list = []

    class RecordingModel:
        def __init__(self):
            self.script = iter(
                [
                    {"tool": "list_files", "args": {}},
                    {"tool": "finish", "args": {"success": True, "summary": "s"}},
                ]
            )

        def complete(self, messages, tools):
            captured.append([dict(m) for m in messages])
            step = next(self.script)
            from app.llm.base import AssistantTurn, ToolCall

            if "tool" in step:
                return AssistantTurn(
                    tool_calls=[ToolCall(id="call_1", name=step["tool"], arguments=step["args"])],
                    finish_reason="tool_calls",
                    usage_tokens=1,
                )
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call_2",
                        name="finish",
                        arguments={"success": True, "summary": "s"},
                    )
                ],
                finish_reason="tool_calls",
                usage_tokens=1,
            )

    outcome = run_plain_loop(ctx, RecordingModel(), "issue")
    assert outcome.success
    assistant_with_tools = captured[1][2]  # req2 的 messages[2]:turn1 的 assistant 回合
    assert assistant_with_tools["role"] == "assistant"
    call = assistant_with_tools["tool_calls"][0]
    assert call["type"] == "function"
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "list_files"
    assert json.loads(call["function"]["arguments"]) == {}


def test_plain_loop_enforces_time_budget_at_turn_boundary(ctx: ToolContext) -> None:
    """N-10/R2 整改:时间预算必须在每个 turn 边界复查,而不是只有进入循环前一次——
    一次 LLM 调用 + 一次 pytest 可远超剩余额度,锁 TTL 会早于任务结束。"""
    script = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "finish", "args": {"success": True, "summary": "done"}},
    ]
    model = FakeLLM(script)
    started = time.monotonic() - 10_000  # 伪起点:任务早已超时
    with pytest.raises(BudgetError, match="time budget"):
        run_plain_loop(
            ctx,
            model,
            "issue",
            max_turns=5,
            started_monotonic=started,
            time_budget_seconds=1,
        )
    # turn 边界在 model.complete 之前:模型不应被调用(事件里只有 start 无 tool)


def test_plain_loop_without_started_monotonic_skips_time_check(ctx: ToolContext) -> None:
    """started_monotonic 未接线(直接构造场景)不得产生假超时(R2 整改)。"""
    script = [{"tool": "finish", "args": {"success": True, "summary": "done"}}]
    outcome = run_plain_loop(ctx, FakeLLM(script), "issue", max_turns=5)
    assert outcome.success and outcome.finish_declared


def test_budget_error_carries_usage(ctx: ToolContext) -> None:
    """N-11 整改:预算异常携带已耗 token/turns,上层才能记回任务级账本。"""
    script = [{"tool": "search_code", "args": {"keyword": f"k{i}"}} for i in range(5)]
    with pytest.raises(BudgetError) as exc_info:
        run_plain_loop(ctx, FakeLLM(script), "issue", max_turns=3)
    assert exc_info.value.turns == 3  # type: ignore[attr-defined]
    assert getattr(exc_info.value, "tokens_spent", 0) > 0


def test_llm_thought_recorded_with_truncation(ctx: ToolContext) -> None:
    """P1-b:推理文本+工具名逐轮入轨迹;超长内容截断并标注原文长度。"""
    model = FakeLLM(
        [
            {"content": "先看看目录。"},
            {"content": "x" * 2500},
            {"tool": "finish", "args": {"success": True, "summary": "s"}},
        ]
    )
    outcome = run_plain_loop(ctx, model, "issue")
    assert outcome.success

    assert ctx.tracker.path is not None
    events = [
        json.loads(line) for line in ctx.tracker.path.read_text(encoding="utf-8").splitlines()
    ]
    llm = [e for e in events if e["tool"] == "llm"]
    assert [e["input"]["turn"] for e in llm] == [1, 2, 3]
    assert llm[0]["output_summary"]["content"] == "先看看目录。"
    assert llm[0]["output_summary"]["tool_calls"] == []
    truncated = llm[1]["output_summary"]["content"]
    assert truncated.startswith("x" * 100) and "(2500 chars)" in truncated
    assert len(truncated) < 2500
    assert llm[2]["output_summary"]["tool_calls"] == ["finish"]


class _ScriptedModel:
    """按预置回合吐 AssistantTurn,并记下每次收到的 messages(离线,不联网)。"""

    provider = "scripted"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append([dict(m) for m in messages])
        return self.turns[len(self.seen) - 1]


def _two_turn_model(reasoning: str | None) -> _ScriptedModel:
    from app.llm.base import ToolCall

    return _ScriptedModel(
        [
            AssistantTurn(
                content="先读文件",
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "src/dateparse.py"})
                ],
                reasoning_content=reasoning,
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c2", name="finish", arguments={"success": True, "summary": "好了"})
                ]
            ),
        ]
    )


def test_loop_passes_reasoning_content_back_on_next_turn(ctx: ToolContext) -> None:
    """思考模式的思维链必须回传:端点缺了它就 400,整题崩成 NEEDS_REVIEW。"""
    model = _two_turn_model("我在想失败测试为什么断言不成立")
    outcome = run_plain_loop(ctx, model, "issue")

    assert outcome.finish_declared
    second_request = model.seen[1]
    assistants = [m for m in second_request if m.get("role") == "assistant"]
    assert assistants, "第二轮请求里应带上上一轮的 assistant 消息"
    assert assistants[0]["reasoning_content"] == "我在想失败测试为什么断言不成立"
    assert assistants[0]["tool_calls"], "回传时不能丢工具调用"


def test_loop_message_shape_unchanged_without_reasoning(ctx: ToolContext) -> None:
    """非思考端点:assistant 消息里不得凭空多出 reasoning_content 键。"""
    model = _two_turn_model(None)
    run_plain_loop(ctx, model, "issue")

    assistants = [m for m in model.seen[1] if m.get("role") == "assistant"]
    assert assistants
    assert "reasoning_content" not in assistants[0]


# ---------- M1 上下文滑动窗口压缩接线 ----------


class _RecordingFakeLLM(FakeLLM):
    """FakeLLM + 记下每次收到的 messages:压缩是否真的变小只能对着请求核。"""

    def __init__(self, script: list[dict]) -> None:
        super().__init__(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append([dict(m) for m in messages])
        return super().complete(messages, tools)


def _read_script(n_reads: int) -> list[dict]:
    """只读调查脚本:连续 n 次 read_file(实测死因就是这类轮次把额度烧光)。"""
    return [
        {"tool": "read_file", "args": {"path": "src/dateparse.py", "offset": offset}}
        for offset in range(1, n_reads + 1)
    ] + [{"tool": "finish", "args": {"success": True, "summary": "已经查过了"}}]


def _seed_tokens(issue_text: str) -> int:
    """system + 首条 user 的固定开销:压缩阈值围绕它取,换提示词也不用改用例。"""
    return messages_tokens(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": issue_text},
        ]
    )


def _readonly_ctx(ctx: ToolContext, task_id: str, tmp_path: Path) -> ToolContext:
    """同一工作区的第二个上下文(只读工具不脏工作区),轨迹另写一份便于开关对跑。"""
    return ToolContext(
        task_id=task_id,
        workspace=ctx.workspace,
        baseline_commit=ctx.baseline_commit,
        tracker=Tracker(tmp_path / f"{task_id}.jsonl", task_id=task_id),
        report_dir=ctx.report_dir,
        test_sets=dict(ctx.test_sets),
    )


ISSUE = "空字符串未防御,应返回 None"


def test_loop_compacts_context_still_finishes_and_shrinks_requests(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """接线主用例:超过软阈值 → 压缩 → 循环照常 finish,发出去的消息确实变小。"""
    threshold = _seed_tokens(ISSUE) + 1300

    model = _RecordingFakeLLM(_read_script(8))
    outcome = run_plain_loop(
        ctx,
        model,
        ISSUE,
        max_turns=20,
        context_window_tokens=threshold,
        context_keep_recent_turns=2,
    )
    baseline = _RecordingFakeLLM(_read_script(8))
    run_plain_loop(_readonly_ctx(ctx, "T-NO-COMPACT", tmp_path), baseline, ISSUE, max_turns=20)

    assert outcome.finish_declared and outcome.success
    events = [e for e in ctx.tracker.events if e.tool == "context_compact"]
    assert events, "压缩必须留一条轨迹事件"
    first = events[0]
    assert first.input["threshold"] == threshold
    assert first.input["before_tokens"] > threshold
    assert first.output_summary["stubbed"] > 0  # type: ignore[index]
    assert first.output_summary["state"] == "LOOP"  # type: ignore[index]
    assert first.output_summary["turn"] >= 1  # type: ignore[index]

    # 压缩后的请求确实更小(不压缩时历史单调增长到最后一轮)
    assert messages_tokens(model.seen[-1]) < messages_tokens(baseline.seen[-1])
    assert sum(1 for m in model.seen[-1] if m.get("role") == "tool") == 8
    stubs = [m for m in model.seen[-1] if str(m.get("content", "")).startswith(STUB_PREFIX)]
    assert stubs, "最旧的观察应以存根留在场内,而不是凭空消失"
    tail_tools = [m for m in model.seen[-1] if m.get("role") == "tool"][-2:]
    for msg in tail_tools:  # pinned 尾部:模型正在用的观察一字不动
        assert not str(msg["content"]).startswith(STUB_PREFIX)
    for event in events:
        assert event.output_summary["after_tokens"] == messages_tokens(  # type: ignore[index]
            model.seen[int(event.output_summary["turn"]) - 1]  # type: ignore[index]
        )


def test_loop_compaction_keeps_reasoning_field_present(ctx: ToolContext) -> None:
    """思考模式端点缺 reasoning_content 就 400:被压的回合必须仍是"字段存在、内容存根"。"""
    from app.llm.base import ToolCall

    threshold = _seed_tokens(ISSUE) + 1300
    reasoning = "我在想失败测试为什么断言不成立,先读文件确认空串分支" * 2
    turns = [
        AssistantTurn(
            content=f"第 {offset} 次读",
            tool_calls=[
                ToolCall(
                    id=f"c{offset}",
                    name="read_file",
                    arguments={"path": "src/dateparse.py", "offset": offset},
                )
            ],
            reasoning_content=reasoning,
            usage_tokens=20,
        )
        for offset in range(1, 9)
    ] + [
        AssistantTurn(
            tool_calls=[
                ToolCall(id="cfin", name="finish", arguments={"success": True, "summary": "好了"})
            ],
            usage_tokens=20,
        )
    ]
    model = _ScriptedModel(turns)

    outcome = run_plain_loop(
        ctx,
        model,
        ISSUE,
        max_turns=20,
        context_window_tokens=threshold,
        context_keep_recent_turns=2,
    )

    assert outcome.finish_declared
    last = model.seen[-1]
    assistants = [m for m in last if m.get("role") == "assistant"]
    assert len(assistants) == 8, "本轮是 Pass A(存根化),不该整组丢历史"
    assert all("reasoning_content" in m for m in assistants), "字段消失 = 思考端点 400"
    assert any(m["reasoning_content"] == REASONING_STUB for m in assistants)
    assert assistants[-1]["reasoning_content"] == reasoning  # pinned 尾部原样


def test_loop_compaction_does_not_weaken_token_budget(ctx: ToolContext, tmp_path: Path) -> None:
    """门禁不变量:同一额度下,不压缩必死、压缩能活;额度压不穿。"""
    threshold = _seed_tokens(ISSUE) + 700
    budget = _seed_tokens(ISSUE) + 1150

    with pytest.raises(BudgetError) as info:
        run_plain_loop(
            _readonly_ctx(ctx, "T-NO-COMPACT-BUDGET", tmp_path),
            _RecordingFakeLLM(_read_script(8)),
            ISSUE,
            max_turns=20,
            token_budget=budget,
        )
    assert "exceed budget" in str(info.value)

    model = _RecordingFakeLLM(_read_script(8))
    outcome = run_plain_loop(
        ctx,
        model,
        ISSUE,
        max_turns=20,
        token_budget=budget,
        context_window_tokens=threshold,
        context_keep_recent_turns=2,
    )
    assert outcome.finish_declared
    assert any(e.tool == "context_compact" for e in ctx.tracker.events)


def test_loop_still_raises_budget_error_when_compaction_cannot_help(ctx: ToolContext) -> None:
    """压无可压(首轮只有 pinned 区)时照旧 BudgetError,语义一字未改。"""
    with pytest.raises(BudgetError) as info:
        run_plain_loop(
            ctx,
            _RecordingFakeLLM(_read_script(4)),
            ISSUE,
            max_turns=20,
            token_budget=_seed_tokens(ISSUE) - 10,
            context_window_tokens=200,
            context_keep_recent_turns=2,
        )
    assert "exceed budget" in str(info.value)


def test_loop_context_window_zero_reproduces_old_message_sequence(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """回归钉子:**显式关闭**(context_window_tokens=0)时历史只增不减,行为与此前逐字一致。

    M1.5 之后 Settings 的生产默认不再是 0(见 test_loop_context_defaults_follow_settings),
    所以这条钉子必须自己传 0,不能再借"默认值"的名义——它钉的是"关掉压缩时不能有任何差异",
    而"关"现在是一个需要显式选择的档位。
    """
    model = _RecordingFakeLLM(_read_script(6))
    outcome = run_plain_loop(
        ctx,
        model,
        ISSUE,
        max_turns=20,
        context_window_tokens=0,
        context_keep_recent_turns=2,
    )
    explicit_zero = _RecordingFakeLLM(_read_script(6))
    run_plain_loop(
        _readonly_ctx(ctx, "T-EXPLICIT-ZERO", tmp_path),
        explicit_zero,
        ISSUE,
        max_turns=20,
        context_window_tokens=0,
        context_keep_recent_turns=2,
    )

    assert outcome.finish_declared
    assert [e.tool for e in ctx.tracker.events].count("context_compact") == 0
    assert explicit_zero.seen == model.seen
    sizes = [len(request) for request in model.seen]
    assert sizes == [2, 4, 6, 8, 10, 12, 14], "不压缩时消息条数应单调增长"
    for previous, following in zip(model.seen, model.seen[1:], strict=False):
        assert following[: len(previous)] == previous, "每轮请求都是下一轮的前缀"
    for request in model.seen:
        for message in request:
            assert "[compacted]" not in str(message.get("content", ""))


def test_loop_context_defaults_follow_settings(ctx: ToolContext) -> None:
    """阈值默认 None = 跟随 Settings(与 `token_budget` 同一约定),Settings 默认是生产值。

    M1.5 的决定与理由:上下文"只增不减"是实测主死因
    (runs/swe-hard-graph*:16-19 轮只读调查、417,894 tok 撞 400k 份额顶、apply_patch 0 次),
    默认 0 = 关闭等于机制没上线。约定用 None 而不是把 16000 抄进签名:
    任何调用方不传参就跟平台配置走,**不会出现"graph 臂压缩、消融臂不压缩"这种未登记的消融变量**
    (这类缺陷本仓库已为此作废过一次付费实验)。
    """
    import inspect

    from app.config import Settings, get_settings

    settings = get_settings()
    defaults = {
        name: param.default
        for name, param in inspect.signature(run_plain_loop).parameters.items()
        if name in {"context_window_tokens", "context_keep_recent_turns"}
    }
    assert defaults == {"context_window_tokens": None, "context_keep_recent_turns": None}
    assert settings.context_window_tokens == 16_000
    assert settings.context_keep_recent_turns == 6
    # 0 仍是"关闭"档,且 Settings 可以整体退回旧行为
    assert Settings(context_window_tokens=0).context_window_tokens == 0

    with pytest.raises(ValueError, match="context_keep_recent_turns"):
        Settings(context_keep_recent_turns=0)
