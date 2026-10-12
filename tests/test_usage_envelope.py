"""G02 计量协议用例:usage 的来源、缓存子集、异常携带用量与请求估算。

对应 SPEC §7 G02 的测试清单与验收矩阵 T03/T04(离线子集)。这一批用例钉的是
"读数说了什么、来源标了什么",不是"额度够不够"——预留与结算是 G03。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import AssistantTurn, messages_tokens
from app.llm.usage import (
    ESTIMATE_METHOD,
    ESTIMATE_VERSION,
    RETRY_PLATFORM,
    RETRY_SDK_OPAQUE,
    USAGE_ESTIMATED,
    USAGE_PROVIDER,
    USAGE_UNKNOWN,
    AttemptRecord,
    ModelResponseEnvelope,
    RequestEstimate,
    UsageEnvelope,
    estimate_request,
    new_attempt_id,
    parse_usage,
)


def test_missing_usage_is_unknown_not_zero() -> None:
    """端点不带 usage:来源 unknown、读数 None,而不是 0 也不 provider。"""
    env = parse_usage(None)
    assert env.usage_source == USAGE_UNKNOWN
    assert env.total_tokens is None and env.chargeable_total is None
    assert "usage_absent" in env.anomalies


def test_standard_cached_tokens_are_a_subset_of_prompt() -> None:
    """OpenAI 风格:cached_tokens 是 prompt 的子集,入账总额不得把它再加一遍。"""
    raw = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=600),
    )
    env = parse_usage(raw)
    assert env.usage_source == USAGE_PROVIDER
    assert env.cached_tokens == 600
    assert env.uncached_input_tokens == 400
    assert env.chargeable_total == 1200  # 1000+200,不是 1000+200+600
    assert env.cache_fields == ("prompt_tokens_details.cached_tokens",)
    assert env.anomalies == []


def test_deepseek_style_hit_and_miss_are_recognised() -> None:
    """DeepSeek 风格:命中/未命中直接成对声明,分区与 prompt 对得上就不报异常。"""
    raw = {"prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}
    env = parse_usage(raw)
    assert env.cached_tokens == 700
    assert env.uncached_tokens == 300
    assert env.uncached_input_tokens == 300
    assert set(env.cache_fields) == {"prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}


def test_cache_partition_bigger_than_prompt_is_an_anomaly() -> None:
    """缓存明细超过 prompt:读数留着,但异常必须登记(不许静默合理化)。"""
    raw = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        prompt_tokens_details=SimpleNamespace(cached_tokens=400),
    )
    env = parse_usage(raw)
    assert "cache_exceeds_prompt" in env.anomalies
    assert env.uncached_input_tokens == 0


def test_two_cache_adapters_disagree_takes_the_smaller_cached() -> None:
    """两套写法同时存在且不一致:按较小的 cached 入账(未缓存输入价更高,宁可多算)。"""
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_cache_hit_tokens": 900,
        "prompt_tokens_details": {"cached_tokens": 400},
    }
    env = parse_usage(raw)
    assert env.cached_tokens == 400
    assert "cache_adapters_disagree" in env.anomalies


def test_total_without_detail_is_a_legal_response() -> None:
    """只返回 total 的端点(SPEC 明列的合法形状):总额入账,明细保持未知。"""
    env = parse_usage(SimpleNamespace(total_tokens=321))
    assert env.total_tokens == 321
    assert env.detail_sum is None
    assert env.chargeable_total == 321
    assert env.usage_source == USAGE_PROVIDER


def test_total_detail_mismatch_charges_the_larger_number() -> None:
    """总额与明细不一致:登记异常,并按**较大**者入账(消耗宁可高估)。"""
    env = parse_usage(SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=260))
    assert "total_detail_mismatch" in env.anomalies
    assert env.detail_sum == 150
    assert env.chargeable_total == 260


def test_negative_and_non_numeric_values_are_treated_as_unknown() -> None:
    """负数/字符串不是合法用量:当缺失(None),不许当成 0 也不许猜测。"""
    env = parse_usage({"prompt_tokens": -5, "completion_tokens": "many", "total_tokens": None})
    assert env.prompt_tokens is None and env.completion_tokens is None
    assert env.chargeable_total is None


def test_unknown_fields_keep_a_redacted_copy() -> None:
    """未参与解释的字段留副本:数字照留,像凭据的字符串只留字段名(T24 的公开面)。"""
    env = parse_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "vendor_request_id": "req-7",
            "x_billable_api_key": "sk-live-abcdef",
            "x_cache_ratio": 0.25,
        }
    )
    assert env.extras["vendor_request_id"] == "req-7"
    assert env.extras["x_cache_ratio"] == 0.25
    assert env.extras["x_billable_api_key"] == "<redacted>"
    assert "prompt_tokens" not in env.extras  # 已知字段不进副本


def test_reasoning_tokens_come_from_completion_details() -> None:
    """思考端点的 reasoning 明细:它是 completion 的子集,只作解释不进总额。"""
    env = parse_usage(
        SimpleNamespace(
            completion_tokens=300,
            total_tokens=400,
            prompt_tokens=100,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=180),
        )
    )
    assert env.reasoning_tokens == 180
    assert env.chargeable_total == 400


def test_estimated_and_unknown_factories_carry_their_reason() -> None:
    env = UsageEnvelope.estimated(prompt_tokens=40, completion_tokens=8, reason="endpoint_omitted")
    assert (env.usage_source, env.chargeable_total) == (USAGE_ESTIMATED, 48)
    assert (env.estimate_reason, env.estimate_method, env.estimate_version) == (
        "endpoint_omitted",
        ESTIMATE_METHOD,
        ESTIMATE_VERSION,
    )
    lost = UsageEnvelope.unknown("connection_reset", attempts=2)
    assert lost.usage_source == USAGE_UNKNOWN and lost.unknown_attempts == 2
    assert lost.chargeable_total is None


def test_legacy_turn_without_envelope_is_labelled_estimated() -> None:
    """旧桩/回放脚本的三个 int:如实换算成 estimated,不得默认 provider。"""
    turn = AssistantTurn(content="x", usage_tokens=100_000)
    env = UsageEnvelope.from_turn_ints(
        prompt_tokens=turn.prompt_tokens,
        completion_tokens=turn.completion_tokens,
        total_tokens=turn.usage_tokens,
    )
    assert env.usage_source == USAGE_ESTIMATED
    assert env.chargeable_total == 100_000
    assert env.estimate_reason == "turn_without_envelope"


def test_zero_only_turn_keeps_the_historical_charge_of_zero() -> None:
    """全是 0 的旧回合(桩里常见):换算后无可入账量,与既有台账读数一致。"""
    env = UsageEnvelope.from_turn_ints(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    assert env.chargeable_total is None
    assert "usage_absent" in env.anomalies


def test_combine_adds_known_usage_and_keeps_unknown_attempt_count() -> None:
    """多物理尝试合成:已知量相加,未知那几次只记笔数不记零。"""
    merged = UsageEnvelope.combine(
        [
            parse_usage(SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120)),
            UsageEnvelope.unknown("connection_reset"),
        ]
    )
    assert merged.chargeable_total == 120
    assert merged.unknown_attempts == 1
    assert merged.usage_source == USAGE_PROVIDER  # 唯一已知来源保留其标签


def test_combine_downgrades_mixed_sources_to_estimated() -> None:
    """已知部分来源不一致:整体降为 estimated,并登记混合来源异常。"""
    merged = UsageEnvelope.combine(
        [
            parse_usage(SimpleNamespace(total_tokens=100)),
            UsageEnvelope.estimated(prompt_tokens=10, completion_tokens=5, reason="x"),
        ]
    )
    assert merged.usage_source == USAGE_ESTIMATED
    assert merged.chargeable_total == 115
    assert "mixed_usage_sources" in merged.anomalies
    assert "mixed_sources" in merged.estimate_reason


def test_combine_of_all_unknown_is_not_zero_consumption() -> None:
    merged = UsageEnvelope.combine(
        [UsageEnvelope.unknown("timeout"), UsageEnvelope.unknown("timeout")]
    )
    assert merged.usage_source == USAGE_UNKNOWN
    assert merged.chargeable_total is None
    assert merged.unknown_attempts == 2


def test_request_estimate_counts_schema_arguments_and_reasoning() -> None:
    """请求估算计入:有效 schema、tool arguments、需回传的 reasoning 与协议常数。"""
    messages = [
        {"role": "system", "content": "s" * 400},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "r" * 800,
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
    ]
    estimate = estimate_request(messages, [{"type": "function", "function": {"name": "finish"}}])
    assert estimate.content_tokens > 0
    assert estimate.reasoning_tokens > 0, "reasoning 要回传就是真实容量"
    assert estimate.tool_argument_tokens > 0
    assert estimate.schema_tokens > 0
    assert estimate.total_tokens > estimate.content_tokens
    assert estimate.method == ESTIMATE_METHOD and estimate.version == ESTIMATE_VERSION


def test_request_estimate_does_not_touch_the_gate_reading() -> None:
    """估算扩展不得改动额度闸的读数:`messages_tokens` 的算法一字未动(M15/M17 锚点)。"""
    messages = [
        {"role": "user", "content": "u" * 80},
        {
            "role": "assistant",
            "content": "a" * 80,
            "reasoning_content": "r" * 400,
            "tool_calls": [{"id": "c", "function": {"arguments": '{"x": 1}'}}],
        },
    ]
    gate = messages_tokens(messages)
    estimate = estimate_request(messages, [])
    assert estimate.content_tokens + estimate.tool_argument_tokens == gate
    assert estimate.reasoning_tokens > 0  # 差额正是闸门漏算的那部分
    assert estimate.protocol_tokens == 2 * 4  # 每条消息的框架开销


def test_envelope_report_dict_never_leaks_thinking_or_secrets() -> None:
    """公开面读数:有计量事实,没有思维链原文,也没有凭据样字段值(T24)。"""
    env = ModelResponseEnvelope(
        usage=parse_usage(SimpleNamespace(total_tokens=10, prompt_tokens=6, completion_tokens=4)),
        finish_reason="stop",
        attempts=[
            AttemptRecord(
                index=1,
                attempt_id=new_attempt_id(),
                status="ok",
                usage=parse_usage(SimpleNamespace(total_tokens=10)),
                retry_owner=RETRY_PLATFORM,
            )
        ],
        thinking_requested="low",
        reasoning_returned=True,
    )
    report = env.as_report_dict()
    assert report["usage_source"] == USAGE_PROVIDER
    assert report["physical_attempts"][0]["retry_owner"] == RETRY_PLATFORM
    assert report["reasoning_returned"] is True
    assert "reasoning_content" not in str(report)
    assert env.physical_attempts == 1


def test_sdk_opaque_attempt_records_its_unobservable_bound() -> None:
    """SDK 隐式重试:平台只能给上界与"看不见"这个事实,不补造历史用量。"""
    attempt = AttemptRecord(
        index=1,
        attempt_id=new_attempt_id(),
        status="ok",
        usage=UsageEnvelope.unknown("sdk_retries_inside_one_call"),
        retry_owner=RETRY_SDK_OPAQUE,
        sdk_retry_bound=2,
    )
    assert attempt.as_dict()["sdk_retry_bound"] == 2
    assert attempt.as_dict()["usage_source"] == USAGE_UNKNOWN


def test_attempt_ids_are_distinct() -> None:
    assert new_attempt_id() != new_attempt_id()


def test_request_estimate_defaults_are_zeroed_and_serialisable() -> None:
    estimate = RequestEstimate()
    assert estimate.total_tokens == 0
    assert set(estimate.as_dict()) == {
        "content_tokens",
        "tool_argument_tokens",
        "reasoning_tokens",
        "schema_tokens",
        "protocol_tokens",
        "total_tokens",
        "estimate_method",
        "estimate_version",
    }


@pytest.mark.parametrize("bad", [object(), "not-a-usage", 7])
def test_unparsable_usage_is_unknown_instead_of_crash(bad: object) -> None:
    """usage 字段形态不认识:如实 unknown,绝不让一次计量把整题炸掉。"""
    env = parse_usage(bad)
    assert env.usage_source == USAGE_UNKNOWN
    assert env.chargeable_total is None


# ---------- G02 接线:异常先计量、归类进轨迹、公开面去敏 ----------


def _ctx(tmp_path, task_id: str = "T-USAGE"):
    from app.tools.base import ToolContext
    from app.tools.tracker import Tracker

    return ToolContext(
        task_id=task_id,
        workspace=tmp_path / "ws",
        baseline_commit="",
        tracker=Tracker(None, task_id=task_id),
        report_dir=tmp_path / "reports",
    )


def _envelope_with(**kwargs: Any) -> ModelResponseEnvelope:
    from app.llm.usage import AttemptRecord

    usage = parse_usage(SimpleNamespace(total_tokens=kwargs.pop("total", 321)))
    return ModelResponseEnvelope(
        usage=usage,
        attempts=[
            AttemptRecord(
                index=1,
                attempt_id=new_attempt_id(),
                status=kwargs.pop("status", "ok"),
                usage=usage,
                retry_owner=kwargs.pop("retry_owner", RETRY_PLATFORM),
            )
        ],
        **kwargs,
    )


class _ParseFailureModel:
    """端点回了 usage 但结构解释不了(T03 的模型侧形状)。"""

    provider = "stub"

    def __init__(self, envelope: ModelResponseEnvelope) -> None:
        self.envelope = envelope

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        from app.errors import ModelResponseError

        raise ModelResponseError("llm endpoint returned no choices", envelope=self.envelope)


def test_parse_failure_books_known_usage_before_propagating(tmp_path) -> None:
    """T03:已知用量**先入账**,异常照常上抛;账本不能把它降格成"什么都没花"。"""
    from app.graph.plain_loop import run_plain_loop
    from app.graph.resources import WITHIN_BUDGET, ResourceLedger

    ctx = _ctx(tmp_path)
    ledger = ResourceLedger(task_id=ctx.task_id, token_limit=10_000)
    model = _ParseFailureModel(_envelope_with(parse_failures=["choices_missing"]))

    with pytest.raises(Exception, match="no choices"):
        run_plain_loop(ctx, model, "issue", max_turns=2, token_budget=0, ledger=ledger)

    assert ledger.tokens_used == 321
    assert ledger.resource_status == WITHIN_BUDGET
    record = next(iter(ledger.calls.values()))
    assert (record.status, record.usage_source) == ("recorded", USAGE_PROVIDER)
    events = [e.tool for e in ctx.tracker.events]
    assert "model_response_invalid" in events
    detail = next(e for e in ctx.tracker.events if e.tool == "model_response_invalid")
    assert detail.output_summary["parse_failures"] == ["choices_missing"]
    assert detail.output_summary["known_tokens"] == 321


class _UnknownUsageModel:
    provider = "stub"

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        raise ConnectionError("连接都没建立")


def test_transport_failure_is_still_unknown_not_zero(tmp_path) -> None:
    """没有 envelope 的真失败:沿用 unknown 语义,不写 0、不重授余量(G03 的前提)。"""
    from app.graph.plain_loop import run_plain_loop
    from app.graph.resources import ResourceLedger

    ctx = _ctx(tmp_path)
    ledger = ResourceLedger(task_id=ctx.task_id, token_limit=10_000)
    with pytest.raises(ConnectionError):
        run_plain_loop(
            ctx, _UnknownUsageModel(), "issue", max_turns=2, token_budget=0, ledger=ledger
        )
    record = next(iter(ledger.calls.values()))
    assert (record.status, record.usage_source) == ("unknown", USAGE_UNKNOWN)
    assert ledger.tokens_used == 0


class _ReplayModel:
    """旧式回合:只有三个 int,没有 envelope(回放脚本与历史桩的形状)。"""

    provider = "fake-replay"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return self.turns[0]


def test_turn_without_envelope_is_recorded_as_estimated(tmp_path) -> None:
    """T04 的标签部分:回放/桩的合成用量不得挂 provider 真值。"""
    from app.graph.plain_loop import run_plain_loop
    from app.graph.resources import ResourceLedger

    ctx = _ctx(tmp_path)
    ledger = ResourceLedger(task_id=ctx.task_id, token_limit=10_000)
    model = _ReplayModel([AssistantTurn(content="done", finish_reason="stop", usage_tokens=120)])
    outcome = run_plain_loop(
        ctx,
        model,
        "issue",
        max_turns=2,
        token_budget=0,
        ledger=ledger,
        complete_on_text=True,
    )

    record = next(iter(ledger.calls.values()))
    assert record.usage_source == USAGE_ESTIMATED
    assert record.total_tokens == 120
    assert outcome.tokens_used == 120


class _EnvelopeModel:
    provider = "stub"

    def __init__(self, envelope: ModelResponseEnvelope, *, tool_calls: list[Any] = ()) -> None:
        self.turn = AssistantTurn(
            content="计划文本",
            tool_calls=list(tool_calls),
            finish_reason="stop",
            usage_tokens=envelope.usage.chargeable_total or 0,
            prompt_tokens=envelope.usage.prompt_tokens or 0,
            completion_tokens=envelope.usage.completion_tokens or 0,
            envelope=envelope,
        )

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        return self.turn


def test_thought_event_carries_metering_classification(tmp_path) -> None:
    """轨迹里的 llm 事件要能回答:这笔数哪来的、发出去几次、是不是被截断。"""
    from app.graph.plain_loop import run_plain_loop

    ctx = _ctx(tmp_path)
    envelope = _envelope_with(total=100, truncated=True, parse_failures=["truncated_completion"])
    run_plain_loop(
        ctx,
        _EnvelopeModel(envelope),
        "issue",
        max_turns=1,
        token_budget=0,
        complete_on_text=True,
    )

    thought = next(e for e in ctx.tracker.events if e.tool == "llm")
    summary = thought.output_summary
    assert summary["usage_source"] == USAGE_PROVIDER
    assert summary["request_kind"] == "normal"
    assert summary["physical_attempts"] == 1
    assert summary["retry_owner"] == [RETRY_PLATFORM]
    assert summary["finish_reason"] == "stop"
    assert "truncated_completion" in summary["parse_failures"]


def test_repair_request_is_labelled_as_such(tmp_path) -> None:
    """协议修正的那一次请求要能被单独数出来(G02:修正请求显式归类)。"""
    from app.graph.plain_loop import run_plain_loop
    from app.llm.base import ToolCall

    class _Scripted:
        provider = "stub"

        def __init__(self) -> None:
            self.turns = [
                AssistantTurn(
                    content="我先调个工具",
                    tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
                    finish_reason="tool_calls",
                    usage_tokens=40,
                ),
                AssistantTurn(content="纯文本计划", finish_reason="stop", usage_tokens=30),
            ]
            self.seen: list[list[dict]] = []

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.seen.append([dict(m) for m in messages])
            return self.turns[len(self.seen) - 1]

    ctx = _ctx(tmp_path, "T-REPAIR-KIND")
    run_plain_loop(ctx, _Scripted(), "issue", max_turns=4, token_budget=0, complete_on_text=True)
    kinds = [e.output_summary["request_kind"] for e in ctx.tracker.events if e.tool == "llm"]
    assert kinds == ["normal", "protocol_repair"]


def test_tracker_redacts_reasoning_and_credentials(tmp_path) -> None:
    """T24:轨迹经 API 原样对外,思维链原文与凭据样字段按键名收口。"""
    from app.tools.tracker import Tracker

    path = tmp_path / "trajectory.jsonl"
    tracker = Tracker(path, task_id="T-REDACT")
    tracker.record(
        tool="llm",
        input_payload={"reasoning_content": "私密思维链"},
        output_summary={
            "content": "可见文本",
            "nested": [{"api_key": "sk-live-xyz", "tokens": 12}],
        },
    )
    line = path.read_text(encoding="utf-8")
    assert "私密思维链" not in line
    assert "sk-live-xyz" not in line
    assert "可见文本" in line and "12" in line
