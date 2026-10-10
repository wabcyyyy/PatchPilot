"""app/llm/openai_client 离线单测:只构造客户端、stub 请求,绝不发起网络调用。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.errors import ModelResponseError, ModelTransportError, TaskError
from app.llm.openai_client import OpenAICompatModel, TransportRetryPolicy, build_model
from app.llm.usage import (
    ATTEMPT_PARSE_FAILED,
    ATTEMPT_TRANSPORT_FAILED,
    USAGE_ESTIMATED,
    USAGE_PROVIDER,
    USAGE_UNKNOWN,
)


class APIConnectionError(Exception):
    """与 openai SDK 同名的桩类:重试判定按键名走(客户端不 import openai)。"""


class BadRequestError(Exception):
    """内容型错误:重发只是再花一遍钱,必须不重试。"""


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_base_url": "http://localhost:9/v1",
        "llm_api_key": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_build_model_rejects_openai_without_config() -> None:
    with pytest.raises(TaskError):
        build_model("openai", Settings(llm_base_url="", llm_api_key=""))


def test_build_model_rejects_unknown_provider() -> None:
    with pytest.raises(TaskError):
        build_model("anthropic", _settings())


def test_build_model_openai_disabled_by_default() -> None:
    """总开关默认关闭:即使配好凭据,openai 提供方也必须显式开启。"""
    with pytest.raises(TaskError) as exc_info:
        build_model("openai", _settings(llm_enabled=False))
    assert "PATCHPILOT_LLM_ENABLED" in str(exc_info.value)


def test_build_model_openai_enabled_constructs_offline() -> None:
    model = build_model("openai", _settings(llm_enabled=True, llm_model="test-model"))
    assert model.model_name == "test-model"
    assert model.provider == "openai"


def test_client_uses_configured_timeout_and_retries() -> None:
    model = OpenAICompatModel(
        _settings(llm_enabled=True, llm_timeout_seconds=7.5, llm_max_retries=0)
    )
    assert model._client.timeout == 7.5
    assert model._client.max_retries == 0


def test_complete_sends_max_tokens_and_maps_response(monkeypatch: pytest.MonkeyPatch) -> None:
    model = OpenAICompatModel(
        _settings(llm_enabled=True, llm_model="test-model", llm_max_tokens=1234)
    )
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="read_file", arguments='{"path": "src/a.py"}'),
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        choice = SimpleNamespace(message=message, finish_reason="tool_calls")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=321))

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    turn = model.complete([{"role": "user", "content": "hi"}], [])

    assert captured["model"] == "test-model"
    assert captured["max_tokens"] == 1234
    assert captured["tools"] is None
    assert turn.finish_reason == "tool_calls"
    assert turn.usage_tokens == 321
    assert turn.is_tool_call
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "src/a.py"}


def test_complete_handles_bad_json_arguments_and_missing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = OpenAICompatModel(_settings(llm_enabled=True))

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(content="纯文本回复", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    turn = model.complete([{"role": "user", "content": "hi"}], [])

    assert turn.content == "纯文本回复"
    assert not turn.is_tool_call
    assert turn.usage_tokens > 0  # usage 缺失时回退到字符估算


def test_complete_parses_prompt_completion_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """N2a:usage 三元组(prompt/completion/total)全部解析进回合。"""
    model = OpenAICompatModel(_settings(llm_enabled=True))
    usage = SimpleNamespace(total_tokens=321, prompt_tokens=100, completion_tokens=221)
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage
    )
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: response)
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.usage_tokens == 321
    assert turn.prompt_tokens == 100
    assert turn.completion_tokens == 221


def test_complete_without_usage_estimates_token_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2 整改:usage 缺失时 prompt/completion 明细走字符估算,不再恒 0——
    此前成本核算对返回 usage 不全的端点(deepseek-r1 等)系统性失真。"""
    model = OpenAICompatModel(_settings(llm_enabled=True))
    message = SimpleNamespace(content="回复内容", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
    )
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: response)
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.usage_tokens > 0
    assert turn.usage_tokens == turn.prompt_tokens + turn.completion_tokens
    assert turn.prompt_tokens > 0 and turn.completion_tokens > 0


def test_fake_llm_token_detail_is_estimate_only() -> None:
    """FakeLLM:prompt=0,completion=估算值,与 usage_tokens 一致。"""
    from app.llm.fake import FakeLLM

    turn = FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "done"}}]).complete(
        [{"role": "user", "content": "hi"}], []
    )
    assert turn.prompt_tokens == 0
    assert turn.completion_tokens == turn.usage_tokens > 0


def test_settings_rejects_unknown_thinking_value() -> None:
    with pytest.raises(ValueError):
        Settings(llm_thinking="medium")


def test_complete_default_has_no_thinking_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认不传 thinking 参数:行为跟随服务端默认(DeepSeek 为 enabled/high)。"""
    model = OpenAICompatModel(_settings(llm_enabled=True))
    captured: dict[str, Any] = {}
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=1, prompt_tokens=0, completion_tokens=1),
    )
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: captured.update(kw) or response
    )
    model.complete([{"role": "user", "content": "hi"}], [])
    assert captured["extra_body"] is None  # 不设档:不覆盖服务端默认


def test_complete_passes_thinking_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    model = OpenAICompatModel(_settings(llm_enabled=True, llm_thinking="disabled"))
    captured: dict[str, Any] = {}
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=1, prompt_tokens=0, completion_tokens=1),
    )
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: captured.update(kw) or response
    )
    model.complete([{"role": "user", "content": "hi"}], [])
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_complete_passes_thinking_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    model = OpenAICompatModel(_settings(llm_enabled=True, llm_thinking="low"))
    captured: dict[str, Any] = {}
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=1, prompt_tokens=0, completion_tokens=1),
    )
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: captured.update(kw) or response
    )
    model.complete([{"role": "user", "content": "hi"}], [])
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}


def test_client_captures_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """思考模式端点返回的 reasoning_content 必须带在 AssistantTurn 上。

    实测坑:DeepSeek 类端点要求后续请求把思维链原样回传,缺了就 400 整题崩掉,
    而 400 走的是崩溃路径,连花了多少 token 都不入账。
    """
    model = OpenAICompatModel(_settings(llm_model="deepseek-flash"))
    usage = SimpleNamespace(total_tokens=500, prompt_tokens=400, completion_tokens=100)

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(
            content="我要读 src/a.py", tool_calls=None, reasoning_content="先想一下失败原因……"
        )
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage)

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    turn = model.complete([{"role": "user", "content": "hi"}], [])

    assert turn.reasoning_content == "先想一下失败原因……"


def test_client_forwards_the_phase_schema_verbatim_and_drops_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G01 允许的**唯一**客户端改动面:空能力集合时省略请求里的 tools 字段。

    客户端必须原样转发"本阶段可见 schema",不许在传输层把全量加回来——否则阶段权限
    在最后一米被推翻。计量与重试的重构是 G02,这里不碰。
    """
    from app.tools.registry import tool_schemas

    model = OpenAICompatModel(_settings(llm_model="test-model"))
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        message = SimpleNamespace(content="1. 目标文件", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(total_tokens=9, prompt_tokens=6, completion_tokens=3),
        )

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)

    model.complete([{"role": "user", "content": "hi"}], tool_schemas(["finish"]))
    assert [item["function"]["name"] for item in captured["tools"]] == ["finish"]

    model.complete([{"role": "user", "content": "hi"}], tool_schemas([]))
    assert captured["tools"] is None, "空能力集合要整个省掉字段,而不是发空数组"
    assert captured["max_tokens"], "省略 tools 不得顺手改动其他请求字段"


def test_client_leaves_reasoning_content_none_when_endpoint_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非思考端点(不返回该字段)行为不变:字段为 None,不会造出空键。"""
    model = OpenAICompatModel(_settings(llm_model="gpt-mock"))
    usage = SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5)

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(content="plain", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage)

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    turn = model.complete([{"role": "user", "content": "hi"}], [])

    assert turn.reasoning_content is None


# ---------- G02:用量优先、解析失败带消耗、物理尝试可观测 ----------


def _response(
    *,
    content: str | None = "ok",
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
    usage: Any = "default",
    reasoning_content: str | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content, tool_calls=tool_calls, reasoning_content=reasoning_content
    )
    raw_usage: Any = (
        SimpleNamespace(total_tokens=321, prompt_tokens=100, completion_tokens=221)
        if usage == "default"
        else usage
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=raw_usage
    )


def _tool_call(arguments: str, *, name: str = "apply_patch") -> SimpleNamespace:
    return SimpleNamespace(id="call_1", function=SimpleNamespace(name=name, arguments=arguments))


def test_usage_is_booked_even_when_choices_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T03:端点带着 usage 回来却给了空 choices —— 已知消耗必须随异常交付。

    此前这里抛裸 TaskError,usage 与异常一起蒸发,账本只剩 unknown:钱花了,
    报告上看不出花过,恢复侧还以为是"连接都没建立"。
    """
    model = OpenAICompatModel(_settings(llm_model="test-model"))
    monkeypatch.setattr(
        model._client.chat.completions,
        "create",
        lambda **kw: SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=321)),
    )

    with pytest.raises(ModelResponseError) as exc_info:
        model.complete([{"role": "user", "content": "hi"}], [])

    envelope = exc_info.value.envelope
    assert envelope.usage.usage_source == USAGE_PROVIDER
    assert envelope.usage.chargeable_total == 321
    assert envelope.parse_failures == ["choices_missing"]
    assert envelope.attempts[0].status == ATTEMPT_PARSE_FAILED


def test_truncated_tool_arguments_carry_usage_and_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """截断的补丁不许执行,但这一次的花费要能查到(复盘 P2 的早失败口径不变)。"""
    model = OpenAICompatModel(_settings(llm_model="test-model"))
    truncated_args = '{"patch_text": "diff --git a/x.py'
    monkeypatch.setattr(
        model._client.chat.completions,
        "create",
        lambda **kw: _response(
            content=None,
            tool_calls=[_tool_call(truncated_args)],
            finish_reason="length",
            usage=SimpleNamespace(total_tokens=99),
        ),
    )

    with pytest.raises(ModelResponseError, match="truncated") as exc_info:
        model.complete([{"role": "user", "content": "hi"}], [])

    envelope = exc_info.value.envelope
    assert envelope.truncated is True
    assert envelope.parse_failures == ["truncated_tool_arguments"]
    assert envelope.usage.chargeable_total == 99


def test_text_only_truncation_is_classified_but_still_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯文本截断仍是一个可用回合(既有行为),但归类字段要显式写出来。"""
    model = OpenAICompatModel(_settings(llm_model="test-model"))
    monkeypatch.setattr(
        model._client.chat.completions,
        "create",
        lambda **kw: _response(content="被截断的纯文本…", finish_reason="length"),
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.envelope is not None
    assert turn.envelope.truncated is True
    assert "truncated_completion" in turn.envelope.parse_failures


def test_unparsable_tool_arguments_are_classified_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具参数不是 JSON 也不是对象:保留原始串交回循环,同时登记解析失败。"""
    model = OpenAICompatModel(_settings(llm_model="test-model"))
    monkeypatch.setattr(
        model._client.chat.completions,
        "create",
        lambda **kw: _response(
            content=None,
            tool_calls=[_tool_call("[1, 2, 3]"), _tool_call("not json at all")],
            finish_reason="tool_calls",
        ),
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.envelope is not None
    assert turn.envelope.parse_failures == ["tool_arguments_invalid", "tool_arguments_invalid"]
    assert all(set(c.arguments) == {"_raw"} for c in turn.tool_calls)


def test_deepseek_cache_fields_never_double_count_in_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek 风格命中/未命中:回合读数仍是 total,不是 total+cached。"""
    model = OpenAICompatModel(_settings(llm_model="deepseek-flash"))
    usage = SimpleNamespace(
        total_tokens=1000,
        prompt_tokens=800,
        completion_tokens=200,
        prompt_cache_hit_tokens=600,
        prompt_cache_miss_tokens=200,
    )
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: _response(usage=usage)
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.usage_tokens == 1000
    assert turn.envelope is not None
    assert turn.envelope.usage.cached_tokens == 600
    assert turn.envelope.usage.uncached_input_tokens == 200
    assert turn.envelope.usage.chargeable_total == 1000


def test_missing_usage_is_labelled_estimated_not_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源标签:端点没给 usage 就是 estimated,不再挂 provider 出厂。"""
    model = OpenAICompatModel(_settings(llm_model="test-model"))
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: _response(usage=None)
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.envelope is not None
    assert turn.envelope.usage.usage_source == USAGE_ESTIMATED
    assert turn.envelope.usage.estimate_reason == "endpoint_omitted_usage"
    assert turn.usage_tokens == turn.prompt_tokens + turn.completion_tokens > 0


def test_retry_ownership_is_recorded_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 上界非 0 时,平台看不见物理尝试次数:如实标 sdk_opaque 并带出上界。"""
    model = OpenAICompatModel(_settings(llm_model="test-model", llm_max_retries=3))
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: _response())
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    attempt = turn.envelope.attempts[0]
    assert attempt.retry_owner == "sdk_opaque"
    assert attempt.sdk_retry_bound == 3

    pinned = OpenAICompatModel(_settings(llm_model="test-model", llm_max_retries=0))
    monkeypatch.setattr(pinned._client.chat.completions, "create", lambda **kw: _response())
    assert pinned.complete([], []).envelope.attempts[0].retry_owner == "platform"


def test_platform_retry_gives_every_physical_attempt_its_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式重试:失败的 attempt 有 ID 与 unknown 用量,成功那次照常计量。"""
    model = OpenAICompatModel(
        _settings(llm_model="test-model", llm_max_retries=0),
        retry_policy=TransportRetryPolicy(max_extra_attempts=1),
    )
    calls: list[int] = []

    def flaky(**kwargs: Any) -> SimpleNamespace:
        calls.append(1)
        if len(calls) == 1:
            raise APIConnectionError("connection reset")
        return _response(
            usage=SimpleNamespace(total_tokens=50, prompt_tokens=30, completion_tokens=20)
        )

    monkeypatch.setattr(model._client.chat.completions, "create", flaky)
    turn = model.complete([{"role": "user", "content": "hi"}], [])

    assert len(calls) == 2, "平台显式重试要真的再发一次"
    envelope = turn.envelope
    assert envelope is not None
    assert [a.status for a in envelope.attempts] == [ATTEMPT_TRANSPORT_FAILED, "ok"]
    assert envelope.attempts[0].attempt_id != envelope.attempts[1].attempt_id
    assert envelope.usage.chargeable_total == 50
    assert envelope.usage.unknown_attempts == 1, "失败那次的花费无从知道"


def test_content_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 类内容错误重发只是再花一遍钱:一次就收,并把消耗交给上层。"""
    model = OpenAICompatModel(
        _settings(llm_model="test-model", llm_max_retries=0),
        retry_policy=TransportRetryPolicy(max_extra_attempts=2),
    )
    calls: list[int] = []

    def refuse(**kwargs: Any) -> SimpleNamespace:
        calls.append(1)
        raise BadRequestError("reasoning_content missing")

    monkeypatch.setattr(model._client.chat.completions, "create", refuse)
    with pytest.raises(ModelTransportError) as exc_info:
        model.complete([{"role": "user", "content": "hi"}], [])

    assert len(calls) == 1
    assert exc_info.value.envelope.usage.unknown_attempts == 1


def test_retry_exhaustion_keeps_every_attempt_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试用尽:两次尝试都在场,合计读数仍是 unknown 而不是 0。"""
    model = OpenAICompatModel(
        _settings(llm_model="test-model", llm_max_retries=0),
        retry_policy=TransportRetryPolicy(max_extra_attempts=1),
    )

    def always_down(**kwargs: Any) -> SimpleNamespace:
        raise APIConnectionError("connection reset")

    monkeypatch.setattr(model._client.chat.completions, "create", always_down)
    with pytest.raises(ModelTransportError) as exc_info:
        model.complete([{"role": "user", "content": "hi"}], [])

    envelope = exc_info.value.envelope
    assert len(envelope.attempts) == 2
    assert envelope.usage.unknown_attempts == 2
    assert envelope.usage.usage_source == USAGE_UNKNOWN


def test_platform_retry_requires_sdk_retries_to_be_off() -> None:
    """两层重试同时开 = 用量无法对账:构造时就拒绝,不留"看着能开"的开关。"""
    with pytest.raises(TaskError, match="LLM_MAX_RETRIES=0"):
        OpenAICompatModel(
            _settings(llm_model="test-model", llm_max_retries=1),
            retry_policy=TransportRetryPolicy(max_extra_attempts=1),
        )


def test_negative_extra_attempts_are_rejected() -> None:
    with pytest.raises(ValueError):
        TransportRetryPolicy(max_extra_attempts=-1)


def test_thinking_protocol_and_request_estimate_are_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking 档位、是否真带回思维链、待发请求估算都进 envelope(公开面无原文)。"""
    model = OpenAICompatModel(_settings(llm_model="deepseek-flash", llm_thinking="low"))
    monkeypatch.setattr(
        model._client.chat.completions,
        "create",
        lambda **kw: _response(reasoning_content="先想一下失败原因……"),
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    envelope = turn.envelope
    assert envelope is not None
    assert envelope.thinking_requested == "low"
    assert envelope.reasoning_returned is True
    assert envelope.request_estimate is not None
    assert envelope.request_estimate.total_tokens > 0

    report = envelope.as_report_dict()
    assert "先想一下失败原因" not in str(report), "T24:思维链原文不进公开读数"
