"""app/llm/openai_client 离线单测:只构造客户端、stub 请求,绝不发起网络调用。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.errors import TaskError
from app.llm.openai_client import OpenAICompatModel, build_model


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


def test_complete_without_usage_zeroes_token_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """usage 缺失时明细回退 0,total 仍走字符估算(现状不变)。"""
    model = OpenAICompatModel(_settings(llm_enabled=True))
    message = SimpleNamespace(content="回复内容", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
    )
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kw: response)
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.usage_tokens > 0
    assert turn.prompt_tokens == 0 and turn.completion_tokens == 0


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
