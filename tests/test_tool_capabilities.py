"""G01 用例:模型可见的工具面必须等于本阶段执行权限,PLAN 必须有单次文本完成契约。

钉住评审 R1/R2 的两条 P1(都在当前代码上由 `docs/evidence/agent-upgrade/probe-before-g01.json`
当场复现过:`plan_plain_text={calls:3, error:"agent loop exceeded max_turns=3"}`、
`plan_visible_schema_count=10`):

1. **可见面 == 执行面**。此前 `run_plain_loop` 无条件把全部 schema 发给模型,阶段白名单只在
   执行侧兜底——模型每越权一次就白烧一轮真实请求。现在 schema 按同一个 `allowed_tools` 装配,
   且执行侧仍独立复核(只隐藏不拦截 = 模型伪造名字就能执行;只拦截不隐藏 = 合规模型也看得见
   用不了的手段)。两条都必须有用例,少一条就是半成品。
2. **PLAN 的完成协议是单次文本**。提示要求"不要调用工具",循环却只认 finish 才退出——遵守提示
   的真实模型只能把阶段份额烧光(离线 marker 特判把这条掩盖成"一轮收敛")。合规纯文本一次请求
   就结束;空/截断/越权最多换一次协议修正;仍不合就结构化降级,不循环烧份额。

用例面只走离线脚本模型,不联网、不起真实端点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.evals.experiment_policy import agent_policy, one_shot_policy
from app.evals.single_shot import ONE_SHOT_TOOLS
from app.gitops.snapshot import create_workspace
from app.graph.nodes import READ_TOOLS, WRITE_TOOLS
from app.graph.plain_loop import MAX_PROTOCOL_REPAIRS, run_plain_loop
from app.llm.base import AssistantTurn, ToolCall
from app.prompts import TEXT_COMPLETION_REPAIR
from app.tools.base import ToolContext
from app.tools.registry import FINISH_TOOL, REGISTRY, tool_schemas
from app.tools.tracker import Tracker

FAILED_ID = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION_IDS = [
    "tests/test_dateparse.py::test_parse_iso_format",
    "tests/test_dateparse.py::test_slash_format",
]
# 计划段的合规产物:一段纯文本,点名文件与分支,不带工具调用(形状取自 PLAN_PROMPT 的四条)
PLAN_TEXT = (
    "1. 目标:src/dateparse.py::parse_date\n"
    "2. 机理:空字符串走进 datetime.strptime 抛 ValueError\n"
    "3. 改动:函数开头加 `if not value.strip(): return None`\n"
    "4. 验证:failed 集全绿,regression 集保持绿"
)
WRITE_SIDE_TOOLS = {"apply_patch", "run_tests", "reset_workspace"}


@pytest.fixture()
def ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    """真 git 工作区(M10 的教训:非 git 目录会让收尾的 diff 判定抛异常,把夹具缺陷藏成假通过)。"""
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    return ToolContext(
        task_id="T-CAP",
        workspace=tmp_path / "ws",
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-CAP"),
        report_dir=tmp_path / "reports",
        test_sets={"failed": [FAILED_ID], "regression": REGRESSION_IDS},
    )


class _Turns:
    """按预置回合吐回复,并记下**每次实际发出的请求**(messages + 可见 schema 名单)。

    SPEC §8 要求"FakeLLM 严格脚本/mock 必须看到最终实际请求和动作",所以这里记的是
    plain_loop 真正发给模型的那份 schema,而不是配置里的白名单常量。
    """

    provider = "scripted"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []
        self.schemas: list[list[str]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.requests.append([dict(m) for m in messages])
        self.schemas.append([str(item.get("name")) for item in tools])
        return self.turns[min(len(self.requests), len(self.turns)) - 1]

    def events(self, ctx: ToolContext) -> list[str]:
        return [e.tool for e in ctx.tracker.events]


def _visible(allowed: list[str] | None) -> set[str]:
    return {str(item["name"]) for item in tool_schemas(allowed)}


def _workspace_untouched(ctx: ToolContext) -> bool:
    """工作树与基线逐字一致——计划段的"零副作用"就是这句,两处断言共用一个口径。"""
    from app.gitops.differ import working_tree_diff

    return working_tree_diff(ctx.workspace).is_empty


# ---------- 可见面 == 执行面 ----------


def test_each_phase_shows_exactly_its_allowed_tools() -> None:
    """四个阶段的可见 schema 逐个等于白名单(多一个、少一个都算失败)。"""
    for allowed in (READ_TOOLS, WRITE_TOOLS, [FINISH_TOOL], ONE_SHOT_TOOLS):
        assert _visible(allowed) == set(allowed)
    assert _visible(None) == set(REGISTRY), "不传白名单仍是全量注册表(对账用途)"


def test_localize_hides_every_write_and_test_tool() -> None:
    """LOCALIZE 看不到任何写/测手段——它过去看得到,只是每次调用都被挡回并烧掉一轮。"""
    assert _visible(READ_TOOLS) & WRITE_SIDE_TOOLS == set()
    assert {"apply_patch", "run_tests", "reset_workspace"} <= set(REGISTRY), (
        "这三个工具是真实存在的能力,看不见是**权限决定**,不是没注册"
    )


def test_reset_workspace_is_hidden_in_every_phase_because_no_whitelist_allows_it() -> None:
    """reset_workspace 注册在案却不在任何阶段白名单里:可见面收窄后它就不再误导模型。"""
    allowed_union = set(READ_TOOLS) | set(WRITE_TOOLS) | set(ONE_SHOT_TOOLS)
    allowed_union |= set(agent_policy().propose_tools) | set(one_shot_policy().propose_tools)
    assert "reset_workspace" not in allowed_union
    assert "reset_workspace" in _visible(None) and "reset_workspace" not in _visible(WRITE_TOOLS)


def test_both_experiment_arms_expose_only_their_registered_policy_tools() -> None:
    """两臂对照的 schema 面就是登记面:one_shot 臂拿不到执行反馈通道,agent 臂能拿到。"""
    assert "run_tests" in _visible(list(agent_policy().propose_tools))
    one_shot_visible = _visible(list(one_shot_policy().propose_tools))
    assert "run_tests" not in one_shot_visible and "reset_workspace" not in one_shot_visible
    assert one_shot_visible == set(one_shot_policy().propose_tools)


def test_unregistered_whitelist_name_fails_loudly_instead_of_hiding_a_tool() -> None:
    """白名单写了注册表里没有的名字必须报错:静默少发一个 schema 比崩更难查(模型看不见、
    真去调又成 unknown tool)。顺带把四个阶段白名单全过一遍。"""
    with pytest.raises(KeyError, match="not registered"):
        tool_schemas(["read_file", "ghost_tool"])
    for allowed in (READ_TOOLS, WRITE_TOOLS, [FINISH_TOOL], ONE_SHOT_TOOLS):
        tool_schemas(list(allowed))
    assert tool_schemas([]) == [], "空能力集合 = 一个都不发(客户端再把字段整个省掉)"


def test_a_forged_unlisted_tool_still_cannot_execute(ctx: ToolContext) -> None:
    """模型伪造一个不在可见 schema 里的工具名也执行不了:可见面收窄不等于放松执行侧。"""
    model = _Turns(
        [
            AssistantTurn(
                content="我先打个补丁",
                tool_calls=[
                    ToolCall(id="c1", name="apply_patch", arguments={"patch_text": "junk"})
                ],
                finish_reason="tool_calls",
            ),
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c2", name="finish", arguments={"success": True, "summary": "x"})
                ]
            ),
        ]
    )
    outcome = run_plain_loop(
        ctx, model, "issue", allowed_tools=READ_TOOLS, state_label="LOCALIZE", max_turns=5
    )

    assert "apply_patch" not in model.schemas[0], "写工具不该出现在 LOCALIZE 的可见面里"
    assert outcome.summary == "x"
    blocked = [e for e in ctx.tracker.events if e.tool == "apply_patch"]
    assert blocked and "not allowed in phase" in (blocked[0].error or "")
    # 零副作用要拿 git 说了算,而不是去数字符串(工作树必须与基线逐字一致)
    from app.gitops.differ import working_tree_diff

    assert working_tree_diff(ctx.workspace).is_empty


# ---------- PLAN 的单次文本完成契约 ----------


def _plan(ctx: ToolContext, turns: list[AssistantTurn], **kw: Any):
    """按计划段的真实参数跑一次循环:白名单只有 finish、契约打开、阶段标签 PLAN。"""
    model = _Turns(turns)
    outcome = run_plain_loop(
        ctx,
        model,
        "## 阶段:计划(第 1 轮)",
        allowed_tools=[FINISH_TOOL],
        complete_on_text=True,
        state_label="PLAN",
        max_turns=kw.pop("max_turns", 5),
        **kw,
    )
    return model, outcome


def test_compliant_plan_text_ends_the_stage_in_one_request(ctx: ToolContext) -> None:
    """T01:遵守提示的纯文本计划一次请求就结束,不是份额耗尽、不是 max_turns。"""
    model, outcome = _plan(ctx, [AssistantTurn(content=PLAN_TEXT, finish_reason="stop")])

    assert model.calls == 1, "合规文本必须一次收束,不许再问第二轮"
    assert outcome.completion == "text" and outcome.success and not outcome.finish_declared
    assert outcome.summary == PLAN_TEXT and outcome.turns == 1
    assert model.schemas[0] == [FINISH_TOOL], "计划段可见 schema 只有 finish"
    assert _workspace_untouched(ctx), "计划段没有任何写工具,工作树必然原样"
    assert "protocol_repair" not in model.events(ctx) and "plan_degraded" not in model.events(ctx)


def test_legacy_text_plus_finish_still_completes_equivalently(ctx: ToolContext) -> None:
    """兼容:S00 起的历史回放把"文本 + finish"当收尾,这条路必须等效可用。"""
    model, outcome = _plan(
        ctx,
        [
            AssistantTurn(
                content=PLAN_TEXT,
                tool_calls=[
                    ToolCall(
                        id="f1", name=FINISH_TOOL, arguments={"success": True, "summary": PLAN_TEXT}
                    )
                ],
                finish_reason="tool_calls",
            )
        ],
    )

    assert model.calls == 1 and outcome.finish_declared and outcome.completion == "finish"
    assert outcome.summary == PLAN_TEXT, "两条收法交出的计划工件必须同形"


def test_plan_tool_request_is_bounded_and_side_effect_free(ctx: ToolContext) -> None:
    """T02:计划段要工具 → 先挡下(不进执行),再确认一次协议;第二轮合规即完成。"""
    model, outcome = _plan(
        ctx,
        [
            AssistantTurn(
                content="我想先读一下文件",
                tool_calls=[
                    ToolCall(id="t1", name="read_file", arguments={"path": "src/dateparse.py"})
                ],
                finish_reason="tool_calls",
            ),
            AssistantTurn(content=PLAN_TEXT, finish_reason="stop"),
        ],
    )

    assert model.calls == 1 + MAX_PROTOCOL_REPAIRS
    assert outcome.completion == "text" and outcome.summary == PLAN_TEXT
    repair = [e for e in ctx.tracker.events if e.tool == "protocol_repair"]
    assert len(repair) == 1 and repair[0].input["reason"] == "illegal_tool"
    assert repair[0].output_summary["side_effects"] == 0
    assert model.requests[1][-1]["content"] == TEXT_COMPLETION_REPAIR, "修正只重申协议"
    read_calls = [e for e in ctx.tracker.events if e.tool == "read_file"]
    assert read_calls and read_calls[0].input.get("blocked") is True, "检索工具一次都没真跑"


def test_second_noncompliant_reply_degrades_instead_of_burning_the_share(ctx: ToolContext) -> None:
    """修正额度用尽 → 结构化 plan_invalid(带原因),而不是继续循环到 phase_share/max_turns。"""
    model, outcome = _plan(
        ctx,
        [
            AssistantTurn(
                content="我先看代码",
                tool_calls=[ToolCall(id="t1", name="search_code", arguments={"keyword": "parse"})],
                finish_reason="tool_calls",
            ),
            AssistantTurn(
                content="我再搜一次",
                tool_calls=[
                    ToolCall(id="t2", name="apply_patch", arguments={"patch_text": "junk"})
                ],
                finish_reason="tool_calls",
            ),
        ],
        max_turns=12,
    )

    assert model.calls == 1 + MAX_PROTOCOL_REPAIRS, "有界:绝不走到 max_turns"
    assert outcome.completion == "protocol_invalid" and outcome.protocol_reason == "illegal_tool"
    # 降级交出去的是**最近**一段实质文本(第二轮那句),不是最早那句——上层拿它当暂定计划
    assert not outcome.success and outcome.summary == "我再搜一次"
    assert _workspace_untouched(ctx)


def test_empty_reply_gets_one_repair_then_completes(ctx: ToolContext) -> None:
    model, outcome = _plan(
        ctx,
        [
            AssistantTurn(content="", finish_reason="stop"),
            AssistantTurn(content=PLAN_TEXT, finish_reason="stop"),
        ],
    )

    assert model.calls == 2 and outcome.completion == "text"
    repair = [e for e in ctx.tracker.events if e.tool == "protocol_repair"]
    assert repair[0].input["reason"] == "empty_reply"


def test_truncated_plan_degrades_structurally(ctx: ToolContext) -> None:
    """被 llm_max_tokens 截断的计划不能当成品交给 PROPOSE:两次都截断 → reason=truncated。"""
    model, outcome = _plan(
        ctx,
        [
            AssistantTurn(content="1. 目标:src/dateparse.py", finish_reason="length"),
            AssistantTurn(content="1. 目标:src/dateparse.p", finish_reason="length"),
        ],
    )

    assert model.calls == 1 + MAX_PROTOCOL_REPAIRS
    assert outcome.completion == "protocol_invalid" and outcome.protocol_reason == "truncated"


def test_plan_prompt_and_loop_agree_on_the_completion_contract() -> None:
    """R1 的根子是"提示说别调工具、循环只认 finish"。这里钉住两边现在说的是同一件事。"""
    from app.prompts import PLAN_PROMPT

    assert "完成方式" in PLAN_PROMPT and "纯文本" in PLAN_PROMPT
    assert "不要调用工具" in PLAN_PROMPT
    assert "纯文本" in TEXT_COMPLETION_REPAIR and "不要调用工具" in TEXT_COMPLETION_REPAIR


# ---------- PLAN 节点与整图真的走这条路 ----------


def _settings(**kw: object) -> Any:
    from types import SimpleNamespace

    values: dict[str, object] = {
        "token_budget": 200_000,
        "localize_budget_share": 0.6,
        "plan_stage_enabled": True,
        "plan_budget_share": 0.15,
        "task_timeout_seconds": 900,
        "max_patch_files": 5,
        "test_timeout_seconds": 120,
        "context_window_tokens": 0,
        "context_keep_recent_turns": 6,
    }
    values.update(kw)
    return SimpleNamespace(**values)  # type: ignore[arg-type]


def _nodes(monkeypatch: pytest.MonkeyPatch, ctx: ToolContext, model: Any):
    from app.graph.nodes import TaskNodes

    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: _settings())
    return TaskNodes(
        bug=_bug(),  # type: ignore[arg-type]
        model=model,
        workspace=ctx.workspace,
        tracker=ctx.tracker,
        report_dir=ctx.report_dir,
        max_rounds=5,
        max_turns=10,
        ctx=ctx,
    )


def _bug() -> Any:
    """plan 节点只用得到 issue_text/round_no 这一层,任务身份由 state 带进来。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        id="BUG-CAP",
        issue_text="空字符串应当返回 None",
        failed_tests=[],
        regression_tests=[],
        allowed_paths=None,
        repo_dir=Path("."),
        max_rounds=5,
    )


def _state(**kw: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "round_no": 1,
        "tokens_used": 0,
        "turns": 0,
        "status": "PROPOSE_PATCH",
        "issue_text": "空字符串应当返回 None",
        "findings": "根因:parse_date 未处理空白字符串",
        "feedback": "",
        "plan": "",
    }
    base.update(kw)
    return base


def test_plan_node_completes_on_one_text_request_without_degrading(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """节点级 T01:合规文本 → 一次请求、计划进 state、轨迹里既无 plan_degraded 也无 plan_invalid。"""
    model = _Turns([AssistantTurn(content=PLAN_TEXT, finish_reason="stop")])
    update = _nodes(monkeypatch, ctx, model).plan(_state())

    assert model.calls == 1
    assert update["status"] == "PROPOSE_PATCH" and update["plan"] == PLAN_TEXT
    assert update["turns"] == 1 and "outcome" not in update
    tools = [e.tool for e in ctx.tracker.events]
    assert "plan_degraded" not in tools and "plan_invalid" not in tools
    assert _workspace_untouched(ctx)


def test_plan_node_turns_a_failed_repair_into_a_plan_invalid_event(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """节点级 T02:修正失败是**降级**不是终点——落一条带原因的 plan_invalid,照常进 PROPOSE。"""
    model = _Turns(
        [
            AssistantTurn(
                content="先读一下",
                tool_calls=[ToolCall(id="t1", name="run_tests", arguments={"test_set": "failed"})],
                finish_reason="tool_calls",
            ),
            AssistantTurn(
                content="再跑一次测试",
                tool_calls=[ToolCall(id="t2", name="run_tests", arguments={"test_set": "all"})],
                finish_reason="tool_calls",
            ),
        ]
    )
    nodes = _nodes(monkeypatch, ctx, model)
    update = nodes.plan(_state())

    assert model.calls == 1 + MAX_PROTOCOL_REPAIRS
    assert update["status"] == "PROPOSE_PATCH" and update["plan"] == "再跑一次测试"
    assert nodes.route_plan(update) == "continue", "计划不合协议不得把整题判死"
    invalid = [e for e in ctx.tracker.events if e.tool == "plan_invalid"]
    assert len(invalid) == 1 and invalid[0].state == "PLAN"
    assert invalid[0].output_summary["reason"] == "illegal_tool"
    assert "plan protocol invalid" in (invalid[0].error or "")
    assert _workspace_untouched(ctx), "计划段里 run_tests 被要了两次,一次都没跑"


def test_graph_task_resolves_with_a_strict_plan_replay(tmp_path: Path) -> None:
    """整图证据:严格脚本下计划真的消耗一步、只发一次请求,resolved 仍只由门禁+双测试集决定。

    这条与 `tests/test_plan_stage.py` 的 legacy 旗舰用例成对:同一份题、同一条状态机,
    区别只在 FakeLLM 是否特判计划请求。两条都绿才叫"兼容旧回放 + 新契约不靠特判证明"。
    """
    from app.evals.bugset import load_bug
    from app.graph.runner import run_task_graph
    from app.llm.fake import FakeLLM
    from tests.test_plan_stage import _LOCALIZE, _fix_block, _llm_states, _propose

    script = [*_LOCALIZE, {"content": PLAN_TEXT}, *_propose(_fix_block(), with_run_tests=True)]
    model = FakeLLM(list(script), plan_mode="strict")
    result = run_task_graph(load_bug("BUG-001", Path("bugs")), model, runs_root=tmp_path / "runs")

    assert result.status == "FINISHED" and result.verdict == "resolved"
    states = _llm_states(Path(result.run_dir))
    assert states.count("PLAN") == 1, "计划段一次请求收束(不再是份额耗尽)"
    assert model.consumed == len(script), "严格模式下计划步骤按普通脚本消耗"
    text = (Path(result.run_dir) / "trajectory.jsonl").read_text(encoding="utf-8")
    assert "plan_degraded" not in text and "plan_invalid" not in text
    assert "protocol_repair" not in text


def test_contract_is_opt_in_plain_text_still_loops_by_default(ctx: ToolContext) -> None:
    """默认关:纯文本继续下一轮直到 max_turns。这条既是旧行为钉子,也是 R1 的"改动前形状"。"""
    from app.errors import BudgetError

    model = _Turns([AssistantTurn(content=PLAN_TEXT, finish_reason="stop")])
    with pytest.raises(BudgetError, match="exceeded max_turns"):
        run_plain_loop(
            ctx, model, "issue", allowed_tools=[FINISH_TOOL], state_label="PLAN", max_turns=3
        )
    assert model.calls == 3


# ---------- FakeLLM 的两条计划通道 ----------


def test_strict_fake_llm_replays_a_plain_plan_step_and_legacy_still_autofinishes() -> None:
    """SPEC 明令"不能由 marker 自动补 finish 来证明契约成立":两条通道必须同时在场且形状分明。"""
    from app.llm.fake import FakeLLM
    from app.prompts import PLAN_MARKER

    strict = FakeLLM([{"content": PLAN_TEXT}], plan_mode="strict")
    turn = strict.complete([{"role": "user", "content": f"{PLAN_MARKER}\n计划"}], [])
    assert not turn.is_tool_call and turn.content == PLAN_TEXT
    assert strict.consumed == 1, "严格模式下计划请求按普通脚本步消耗,不再免步"

    legacy = FakeLLM([{"content": "这一步不该被计划请求弹掉"}])
    old = legacy.complete(
        [{"role": "user", "content": f"{PLAN_MARKER}\n### 定位阶段结论\n根因:空值分支"}], []
    )
    assert old.is_tool_call and old.tool_calls[0].name == FINISH_TOOL
    assert legacy.consumed == 0, "legacy 通道的免步契约是既有回放形状的钉子"

    with pytest.raises(ValueError, match="plan_mode"):
        FakeLLM([], plan_mode="strictish")
