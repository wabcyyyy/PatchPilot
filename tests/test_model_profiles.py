"""G07a 模型能力档案与服务端身份采集:能力只能显式声明,观测只留在响应上。

判据口径(SPEC §5 / 验收矩阵 T12):
- 未声明 = 保守档 + `assumed` 清单,**绝不**因为模型名里带 128k 就当它支持 128k;
- 申报比保守上界大又没有来源 → 拒绝发起(不是"先按大窗口跑跑看");
- `served_model` 是一次响应的观测事实,进 envelope/轨迹,不进能力档案冒充声明。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.runtime import (
    REPAIR_V2_KEEP_RECENT_TURNS,
    REPAIR_V2_SOFT_TARGET_TOKENS,
    TaskRuntime,
)
from app.config import Settings
from app.errors import TaskError
from app.llm.openai_client import OpenAICompatModel, build_model
from app.llm.profiles import (
    SOURCE_OPERATOR_CONFIG,
    SOURCE_PROBED,
    SOURCE_UNVERIFIED,
    UNVERIFIED_CAPACITY_TOKENS,
    ModelProfile,
    parse_capabilities,
    profile_from_settings,
)

API_KEY = "sk-secret-material"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_base_url": "https://api.example.com/v1?token=abc",
        "llm_api_key": API_KEY,
        "llm_model": "workhorse-128k",
    }
    values.update(overrides)
    return Settings(**values)


# ---------- 声明与假设 ----------


def test_nothing_is_inferred_from_the_model_name() -> None:
    """模型名叫 128k 也只是名字:没声明就落保守档,并把每一项假设列出来。"""
    profile = profile_from_settings(_settings())
    assert profile.requested_model == "workhorse-128k"
    assert profile.capacity_tokens == UNVERIFIED_CAPACITY_TOKENS
    assert profile.capacity_source == SOURCE_UNVERIFIED
    assert profile.capacity_verified is False
    assert profile.conservative is True
    assert set(profile.assumed) == {
        "capacity_tokens",
        "output_ceiling_tokens",
        "supports_tools",
        "supports_reasoning",
        "reports_cached_usage",
    }


def test_over_claim_without_a_source_is_refused() -> None:
    """给了 128k 却没给来源:拒绝,而不是悄悄改成 16k 再跑。"""
    with pytest.raises(TaskError, match="unverified capacity claim"):
        profile_from_settings(_settings(model_context_tokens=128_000))


def test_a_source_without_a_number_is_a_false_claim() -> None:
    """来源有了容量却是 0:这比"没来源"更危险,因为它看起来像已验证。"""
    with pytest.raises(TaskError, match="MODEL_CONTEXT_TOKENS"):
        profile_from_settings(_settings(model_capacity_source=SOURCE_PROBED))

    with pytest.raises(TaskError, match="requires a positive"):
        ModelProfile(capacity_tokens=0, capacity_source=SOURCE_OPERATOR_CONFIG)


def test_declared_capacity_is_honoured() -> None:
    profile = profile_from_settings(
        _settings(model_context_tokens=64_000, model_capacity_source=SOURCE_OPERATOR_CONFIG)
    )
    assert profile.capacity_tokens == 64_000
    assert profile.capacity_verified is True
    assert "capacity_tokens" not in profile.assumed


def test_a_smaller_guess_is_still_only_a_guess() -> None:
    """保守档以下也算未验证:收紧数字不等于给端点发了合格证。"""
    profile = profile_from_settings(_settings(model_context_tokens=4_000))
    assert profile.capacity_tokens == 4_000
    assert profile.capacity_verified is False


def test_unknown_capacity_source_is_refused() -> None:
    with pytest.raises(TaskError, match="model_capacity_source"):
        profile_from_settings(_settings(model_capacity_source="vibes"))


def test_negative_capacity_is_refused() -> None:
    with pytest.raises(TaskError, match="capacity_tokens must be"):
        ModelProfile(capacity_tokens=-1)


# ---------- 能力清单 ----------


def test_capability_list_is_explicit_and_typos_are_fatal() -> None:
    assert parse_capabilities("") == {}
    assert parse_capabilities("tools=yes, cached_usage=no") == {
        "supports_tools": True,
        "reports_cached_usage": False,
    }
    for bad in ("toolz=yes", "tools=maybe", "tools"):
        with pytest.raises(TaskError, match="model_capabilities entry"):
            parse_capabilities(bad)


def test_declared_capability_leaves_the_assumed_list() -> None:
    profile = profile_from_settings(_settings(model_capabilities="tools=yes,reasoning=no"))
    assert profile.supports_tools is True
    assert profile.supports_reasoning is False
    assert profile.reports_cached_usage is None  # 没声明的那项仍然是不知道
    assert "reports_cached_usage" in profile.assumed


def test_an_endpoint_that_declares_no_tools_is_not_used_for_repair() -> None:
    """不会调工具的端点跑不了这条流水线:拒绝发起,而不是让它聊成一次假修复。"""
    profile = profile_from_settings(_settings(model_capabilities="tools=no"))
    with pytest.raises(TaskError, match="tools=no"):
        profile.assert_usable_for_repair()

    with pytest.raises(TaskError, match="tools=no"):
        build_model("openai", _settings(llm_enabled=True, model_capabilities="tools=no"))


# ---------- 容量与窗口 ----------


def test_capacity_ceiling_subtracts_output_reserve_and_margin() -> None:
    profile = ModelProfile(capacity_tokens=16_000, capacity_source=SOURCE_OPERATOR_CONFIG)
    assert profile.capacity_ceiling() == 16_000
    assert profile.capacity_ceiling(output_reserve=4_096, margin=1_024) == 10_880
    assert profile.capacity_ceiling(output_reserve=99_000, margin=1_024) == 0  # 夹到 0 不给负数


def test_window_is_only_adopted_when_capacity_allows_it() -> None:
    conservative = profile_from_settings(_settings())
    adopted, clamped = conservative.adopt_window(64_000, output_reserve=4_096, margin=1_024)
    assert (adopted, clamped) == (10_880, True), "未验证端点上 64k 目标必须被夹小并留下痕迹"

    # 64k 容量装不下 64k 目标窗口:预留与余量要从容量里扣,这是"不得超过"的字面含义
    just_64k = profile_from_settings(
        _settings(model_context_tokens=64_000, model_capacity_source=SOURCE_PROBED)
    )
    assert just_64k.adopt_window(64_000, output_reserve=4_096, margin=1_024) == (58_880, True)
    assert just_64k.adopt_window(64_000) == (64_000, False), "无预留无余量时目标即上限"


def test_disabled_soft_window_is_not_a_capacity_claim() -> None:
    """target=0 是"关闭软压缩",不是"宣称装得下":原样返回且不算夹过。"""
    profile = profile_from_settings(_settings())
    assert profile.adopt_window(0, output_reserve=4_096) == (0, False)


# ---------- 公开面读数 ----------


def test_as_dict_is_redacted_and_names_the_priced_card() -> None:
    profile = profile_from_settings(_settings(llm_model="gpt-4o-mini"))
    readings = profile.as_dict()
    assert readings["capacity_source"] == SOURCE_UNVERIFIED
    assert readings["capacity_verified"] is False
    assert readings["endpoint"]["host"] == "api.example.com"
    assert readings["endpoint"]["path"] == "/v1"
    assert readings["price_present"] is True
    assert readings["price_version"]
    blob = str(readings)
    assert API_KEY not in blob and "token=abc" not in blob, "档案读数不得带出凭据"


def test_default_settings_keep_todays_transport_shape() -> None:
    assert Settings.model_fields["llm_platform_retries"].default == 0
    assert Settings.model_fields["model_capacity_source"].default == ""
    assert Settings.model_fields["model_capabilities"].default == ""
    assert Settings.model_fields["model_declared_output_ceiling"].default == 0


# ---------- served_model 采集与显式重试装配 ----------


def _response(*, model: str | None = "workhorse-128k", content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=40, prompt_tokens=25, completion_tokens=15),
    )


def test_served_model_is_captured_and_drift_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    model = OpenAICompatModel(_settings(llm_model="workhorse-128k"))
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: _response(model="workhorse-2026-09")
    )
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    envelope = turn.envelope
    assert envelope is not None
    assert envelope.requested_model == "workhorse-128k"
    assert envelope.served_model == "workhorse-2026-09"
    assert envelope.model_identity_drift is True

    report = envelope.as_report_dict()
    assert report["served_model"] == "workhorse-2026-09"
    assert report["model_identity_drift"] is True
    assert API_KEY not in str(report)


@pytest.mark.parametrize("served", ["workhorse-128k", None, ""])
def test_no_drift_when_the_endpoint_agrees_or_says_nothing(
    monkeypatch: pytest.MonkeyPatch, served: str | None
) -> None:
    model = OpenAICompatModel(_settings(llm_model="workhorse-128k"))
    monkeypatch.setattr(
        model._client.chat.completions, "create", lambda **kw: _response(model=served)
    )
    envelope = model.complete([], []).envelope
    assert envelope is not None
    assert envelope.served_model == (served or "")
    assert envelope.model_identity_drift is False


def test_platform_retries_are_assembled_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """G02 留下的开关接进生产构造路径:默认 0 次额外尝试,显式开了就多发一次并单独计量。"""

    class APIConnectionError(Exception):
        """与 openai SDK 同名:重试判定按键名走,客户端不 import openai。"""

    model = OpenAICompatModel(
        _settings(llm_model="workhorse-128k", llm_max_retries=0, llm_platform_retries=1)
    )
    assert model._attempt_budget == 2

    calls: list[int] = []

    def flaky(**kwargs: Any) -> SimpleNamespace:
        calls.append(1)
        if len(calls) == 1:
            raise APIConnectionError("connection reset")
        return _response()

    monkeypatch.setattr(model._client.chat.completions, "create", flaky)
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    envelope = turn.envelope
    assert envelope is not None
    assert len(calls) == 2 and len(envelope.attempts) == 2
    assert envelope.attempts[0].attempt_id != envelope.attempts[1].attempt_id
    assert envelope.usage.unknown_attempts == 1


def test_layered_retries_are_refused_at_construction() -> None:
    """SDK 上界与平台重试同时非 0 = 用量无法对账:构造处拒绝,不留"看着能开"的开关。"""
    with pytest.raises(TaskError, match="LLM_MAX_RETRIES=0"):
        OpenAICompatModel(
            _settings(llm_model="workhorse-128k", llm_max_retries=2, llm_platform_retries=1)
        )


# ---------- G07b 运行 profile:四个概念分开命名,任务开工就冻结 ----------


def test_legacy_v1_is_verbatim_todays_settings() -> None:
    """legacy_v1 = 今日逐字:数字全部照搬 Settings,而且**不做**容量比较。

    这条是回归钉子:profile 层如果悄悄给旧运行加了一道容量闸,历史批次的可比性就没了。
    """
    settings = Settings()
    runtime = TaskRuntime.build(settings)
    assert runtime.profile_name == "legacy_v1" == settings.agent_runtime_profile
    assert runtime.loop_settings() == {
        "context_window_tokens": settings.context_window_tokens,
        "context_keep_recent_turns": settings.context_keep_recent_turns,
        "capacity_ceiling_tokens": 0,
        "runtime_profile": "legacy_v1",
    }
    context = runtime.context
    assert context.soft_compaction_target_tokens == settings.context_window_tokens
    assert context.cumulative_token_budget == settings.token_budget
    assert context.completion_output_cap_tokens == settings.llm_max_tokens
    assert context.capacity_margin_tokens == settings.capacity_margin_tokens
    assert runtime.window.clamped_by_capacity is False
    assert runtime.window.capacity_checked is False


def test_repair_v2_adopts_the_target_window_only_when_capacity_allows_it() -> None:
    runtime = TaskRuntime.build(
        Settings(
            agent_runtime_profile="repair_v2",
            model_context_tokens=128_000,
            model_capacity_source=SOURCE_PROBED,
            llm_max_tokens=8_192,
        )
    )
    assert runtime.context.soft_compaction_target_tokens == REPAIR_V2_SOFT_TARGET_TOKENS == 64_000
    assert runtime.context.keep_recent_turns == REPAIR_V2_KEEP_RECENT_TURNS == 12
    assert runtime.window.clamped_by_capacity is False
    # 保护区判据用的是"容量 − 输出预留 − 余量",与账本同一口径的两端
    assert runtime.window.capacity_ceiling_tokens == 128_000 - 8_192 - 1_024
    assert runtime.loop_settings()["capacity_ceiling_tokens"] == 118_784


def test_repair_v2_on_an_unverified_endpoint_is_clamped_and_says_so() -> None:
    """没有来源的端点跑 v2:64k 目标被夹到保守档的可用余量,并且把"被夹过"留在读数里。

    SPEC §5 的"只在能力声明允许时采用目标窗口"在这里是可核对的:配置里写着 64k,
    实际送进循环的是 10,880 —— 报告必须说真话,不能把愿望写成能力。
    """
    window = TaskRuntime.build(Settings(agent_runtime_profile="repair_v2")).window.as_dict()
    assert window == {
        "target_tokens": 64_000,
        "adopted_tokens": 10_880,
        "clamped_by_capacity": True,
        "capacity_tokens": UNVERIFIED_CAPACITY_TOKENS,
        "capacity_checked": True,
        "capacity_ceiling_tokens": 10_880,
    }


def test_repair_v2_refuses_an_open_ended_output_cap() -> None:
    """v2 不许"输出不限制":没有输出预留就无法判断保护区装不装得下,固定假设 4096 也不行。"""
    with pytest.raises(TaskError, match="explicit llm_max_tokens"):
        TaskRuntime.build(Settings(agent_runtime_profile="repair_v2", llm_max_tokens=0))


def test_repair_v2_refuses_output_above_the_declared_ceiling() -> None:
    with pytest.raises(TaskError, match="declared output ceiling"):
        TaskRuntime.build(
            Settings(
                agent_runtime_profile="repair_v2",
                model_declared_output_ceiling=2_048,
                llm_max_tokens=8_192,
            )
        )


def test_repair_v2_refuses_a_capacity_that_cannot_hold_the_request() -> None:
    """声明容量减掉输出预留与余量后什么都不剩:直接拒绝,不进循环反复压缩。"""
    with pytest.raises(TaskError, match="cannot run"):
        TaskRuntime.build(
            Settings(
                agent_runtime_profile="repair_v2",
                model_context_tokens=4_000,
                llm_max_tokens=4_096,
                capacity_margin_tokens=1_024,
            )
        )


def test_unknown_profile_name_is_refused() -> None:
    with pytest.raises(TaskError, match="agent_runtime_profile"):
        TaskRuntime.build(Settings(), profile_name="repair_v3")
    with pytest.raises(ValueError, match="agent_runtime_profile"):
        Settings(agent_runtime_profile="repair_v3")


def test_profile_is_frozen_at_build_and_two_profiles_coexist() -> None:
    """受理后改环境变量不改在跑的那一份;两个 profile 同时在场也不互相覆盖。

    这条测的是 G07 的"runtime 把冻结的策略实例传入引擎":如果 profile 是每次回读全局
    Settings 得来的,第二个断言就会红 —— 那正是"用改全局配置来模拟不同任务 profile"的缺陷。
    """
    legacy = TaskRuntime.build(Settings())
    repair = TaskRuntime.build(
        Settings(
            agent_runtime_profile="repair_v2",
            model_context_tokens=128_000,
            model_capacity_source=SOURCE_PROBED,
            llm_max_tokens=8_192,
        )
    )
    assert legacy.context.soft_compaction_target_tokens == 16_000
    assert repair.context.soft_compaction_target_tokens == 64_000

    monkeypatched = Settings(
        agent_runtime_profile="repair_v2",
        model_context_tokens=128_000,
        model_capacity_source=SOURCE_PROBED,
        llm_max_tokens=8_192,
        context_window_tokens=1_000,  # 受理后才发现运维把 legacy 的键改了
    )
    assert TaskRuntime.build(monkeypatched).profile_name == "repair_v2"
    assert legacy.context.soft_compaction_target_tokens == 16_000, "已冻结的读数不许被后来者改写"
    assert repair.context.soft_compaction_target_tokens == 64_000


def test_runtime_readings_carry_no_credentials() -> None:
    runtime = TaskRuntime.build(_settings(llm_enabled=True))
    blob = str(runtime.as_dict())
    assert API_KEY not in blob and "token=abc" not in blob
    assert runtime.as_dict()["model"]["capacity_source"] == SOURCE_UNVERIFIED
