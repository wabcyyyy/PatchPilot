"""运行 profile(G07):四类资源各归各位,任务开工就冻结成一份实例。

SPEC §5 把资源分成四类且**互不替代**,而改动前它们在同一批名字里混着读:

| 概念 | 这一层的字段 | 对应 Settings 键 |
|---|---|---|
| 单请求**硬容量**(端点声明) | `ModelProfile.capacity_tokens` | `model_context_tokens` + `model_capacity_source` |
| **软压缩目标**(工作记忆阈值) | `ContextPolicy.soft_compaction_target_tokens` | `context_window_tokens` |
| **累计计算量**(任务额度) | `ContextPolicy.cumulative_token_budget` | `token_budget` |
| **completion 上限**(单次输出) | `ContextPolicy.completion_output_cap_tokens` | `llm_max_tokens` |

Settings 键名保持原样:改名等于改写历史实验身份(SPEC §7 G07"不能改动历史实验身份"),
区分发生在策略对象的字段名与 `as_dict()` 读数上。

两条硬口径:

- **legacy_v1 = 今日逐字**。窗口/钉住轮数/额度/输出上限全部照搬 Settings,不做任何
  容量比较(容量闸在 `ResourceLedger.reserve` 里,只在显式声明容量时生效,G03 的原样)。
  新默认切到 repair_v2 要等 G16 收口,所以这里的默认 profile 名是 legacy_v1。
- **repair_v2 只在能力允许时采用目标窗口**。目标 64k/keep=12 是工程起点而不是结论;
  实际采用值 = min(目标, 声明容量 − 输出预留 − 容量余量),被夹过就留痕。保护区自己
  就装不下时给 `context_capacity_exceeded`,不反复压缩(§5"不得反复压缩直至耗尽预算")。

策略实例是冻结的:受理后改环境变量、或并发跑另一份 profile 的任务,都不会改写已经在跑
的那一份(引擎拿到的是这一份里的数字,不是每次都回读全局 Settings)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.errors import TaskError
from app.llm.profiles import ModelProfile, profile_from_settings

RUNTIME_SCHEMA_VERSION = 1

PROFILE_LEGACY_V1 = "legacy_v1"
PROFILE_REPAIR_V2 = "repair_v2"
RUNTIME_PROFILES = (PROFILE_LEGACY_V1, PROFILE_REPAIR_V2)

# repair_v2 的工程起点(SPEC §5:这是工程起点,尚无效果最优性证据)。离线跑绿不等于
# 64k 对这个端点这批题就是最优——那要 G17 的真实探针来判,不在这里写成结论。
REPAIR_V2_SOFT_TARGET_TOKENS = 64_000
REPAIR_V2_KEEP_RECENT_TURNS = 12

# 保护区超容量时的判据前缀:轨迹/终态里靠它区分"额度花光"与"这一发装不下"
CONTEXT_CAPACITY_EXCEEDED = "context_capacity_exceeded"


@dataclass(frozen=True)
class ContextPolicy:
    """四个互不替代的数字,名字里就带着它们各自是什么。"""

    soft_compaction_target_tokens: int
    keep_recent_turns: int
    completion_output_cap_tokens: int
    cumulative_token_budget: int
    capacity_margin_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "soft_compaction_target_tokens": self.soft_compaction_target_tokens,
            "keep_recent_turns": self.keep_recent_turns,
            "completion_output_cap_tokens": self.completion_output_cap_tokens,
            "cumulative_token_budget": self.cumulative_token_budget,
            "capacity_margin_tokens": self.capacity_margin_tokens,
        }


@dataclass(frozen=True)
class WindowResolution:
    """目标窗口与"能力允许的窗口"相撞时,谁活下来必须留痕。"""

    target_tokens: int
    adopted_tokens: int
    clamped_by_capacity: bool
    capacity_tokens: int  # 端点声明的原始硬容量;0 = 本次不做容量比较
    capacity_checked: bool
    # 单请求**输入**可用余量 = 声明容量 − 输出预留 − 容量余量。循环拿它判"压不动的那一段",
    # 账本拿整发请求(含输出预留)去比它自己的 ceiling(resources.py 的 reserve)。
    capacity_ceiling_tokens: int = 0

    @property
    def ceiling_tokens(self) -> int:
        """循环要用的容量上界:0 = 无可比较容量(此时循环不做保护区容量检查)。"""
        return self.capacity_ceiling_tokens if self.capacity_checked else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_tokens": self.target_tokens,
            "adopted_tokens": self.adopted_tokens,
            "clamped_by_capacity": self.clamped_by_capacity,
            "capacity_tokens": self.capacity_tokens,
            "capacity_checked": self.capacity_checked,
            "capacity_ceiling_tokens": self.capacity_ceiling_tokens,
        }


@dataclass(frozen=True)
class TaskRuntime:
    """一个任务开工时就冻结下来的运行 profile。引擎与工具读它,不回读全局 Settings。"""

    profile_name: str
    model: ModelProfile
    context: ContextPolicy
    window: WindowResolution

    # ---------- 构造 ----------

    @classmethod
    def build(cls, settings: Any, *, profile_name: str | None = None) -> TaskRuntime:
        name = (
            profile_name
            if profile_name is not None
            else str(getattr(settings, "agent_runtime_profile", "") or PROFILE_LEGACY_V1)
        ).strip() or PROFILE_LEGACY_V1
        if name not in RUNTIME_PROFILES:
            raise TaskError(
                f"unknown agent_runtime_profile {name!r}; must be one of {list(RUNTIME_PROFILES)}"
                "(不猜第三种 profile,也不静默退回 legacy)"
            )
        model = profile_from_settings(settings)
        margin = int(getattr(settings, "capacity_margin_tokens", 0) or 0)
        output_cap = int(getattr(settings, "llm_max_tokens", 0) or 0)
        budget = int(getattr(settings, "token_budget", 0) or 0)

        if name == PROFILE_LEGACY_V1:
            context = ContextPolicy(
                soft_compaction_target_tokens=int(
                    getattr(settings, "context_window_tokens", 0) or 0
                ),
                keep_recent_turns=int(getattr(settings, "context_keep_recent_turns", 1) or 1),
                completion_output_cap_tokens=output_cap,
                cumulative_token_budget=budget,
                capacity_margin_tokens=margin,
            )
            # legacy 不做容量比较:今天的运行条件里就没有这一步(G03 的账本闸照旧独立生效)
            window = WindowResolution(
                target_tokens=context.soft_compaction_target_tokens,
                adopted_tokens=context.soft_compaction_target_tokens,
                clamped_by_capacity=False,
                capacity_tokens=0,
                capacity_checked=False,
            )
            return cls(profile_name=name, model=model, context=context, window=window)

        return cls._build_repair_v2(model, output_cap=output_cap, budget=budget, margin=margin)

    @classmethod
    def _build_repair_v2(
        cls,
        model: ModelProfile,
        *,
        output_cap: int,
        budget: int,
        margin: int,
    ) -> TaskRuntime:
        """v2:目标窗口按声明能力采用,输出上限按声明能力配置。"""
        if output_cap <= 0:
            raise TaskError(
                "repair_v2 requires an explicit llm_max_tokens(输出上限要按已声明能力配置,"
                "0=不限制在这里等于没有输出预留,保护区无法判断装不装得下)"
            )
        if model.output_ceiling_tokens > 0 and output_cap > model.output_ceiling_tokens:
            raise TaskError(
                f"llm_max_tokens={output_cap} exceeds the declared output ceiling"
                f" {model.output_ceiling_tokens}(端点声明的输出能力不是可以超的参数)"
            )
        target = REPAIR_V2_SOFT_TARGET_TOKENS
        adopted, clamped = model.adopt_window(target, output_reserve=output_cap, margin=margin)
        if adopted <= 0:
            raise TaskError(
                f"repair_v2 cannot run: declared capacity {model.capacity_tokens} minus output"
                f" reserve {output_cap} and margin {margin} leaves {adopted} tokens for the"
                " request(声明容量装不下这一次请求的输出预留,不反复压缩,直接拒绝)"
            )
        context = ContextPolicy(
            soft_compaction_target_tokens=adopted,
            keep_recent_turns=REPAIR_V2_KEEP_RECENT_TURNS,
            completion_output_cap_tokens=output_cap,
            cumulative_token_budget=budget,
            capacity_margin_tokens=margin,
        )
        window = WindowResolution(
            target_tokens=target,
            adopted_tokens=adopted,
            clamped_by_capacity=clamped,
            capacity_tokens=model.capacity_tokens,
            capacity_checked=True,
            capacity_ceiling_tokens=model.capacity_ceiling(
                output_reserve=output_cap, margin=margin
            ),
        )
        return cls(profile_name=PROFILE_REPAIR_V2, model=model, context=context, window=window)

    # ---------- 消费面 ----------

    def loop_settings(self) -> dict[str, int | str]:
        """`run_plain_loop` 的上下文参数:软阈值、尾部钉住轮数、容量判据与 profile 名。

        两引擎(graph 的四个阶段循环、plain/one-shot 臂)共用这一个出口 —— 同一个 profile
        名字在两条路上必须是同一组数字,否则"换个 profile"就成了一句看引擎才生效的话。
        runtime_policy_hash 是这组数字的指纹(G16 收口 G07):进 loop 快照做绑定,
        恢复方据此拒绝"同名 profile、数值已漂移"的续跑,而不只比名字。
        """
        return {
            "context_window_tokens": self.context.soft_compaction_target_tokens,
            "context_keep_recent_turns": self.context.keep_recent_turns,
            "capacity_ceiling_tokens": self.window.ceiling_tokens,
            "runtime_profile": self.profile_name,
            "runtime_policy_hash": self.policy_hash,
        }

    @property
    def policy_hash(self) -> str:
        """运行 profile 读数的稳定指纹(同名 profile 数值漂移即可检测)。"""
        from app.task_spec import stable_hash

        return stable_hash(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "profile_name": self.profile_name,
            "context": self.context.as_dict(),
            "window": self.window.as_dict(),
            "model": self.model.as_dict(),
        }

    @classmethod
    def from_frozen(cls, policy: dict[str, Any] | None) -> TaskRuntime | None:
        """由受理时冻结的 effective_policy 重建运行 profile(G16 收口 G07)。

        恢复侧必须沿用**受理时**的窗口/输出上限/能力声明,而不是拿当前 Settings 重建
        ——同名 profile 下环境漂移(改了 llm_max_tokens/声明容量)不得改变已受理任务
        的口径。读不出(旧契约没有 runtime_profile 键、或形状不符)返回 None,由调用
        方按兼容约定退回现状;策略 dict 在场但内容非法(如未声明容量超保守上界)按
        构造校验当场抛,不静默换一份。
        """
        if not isinstance(policy, dict):
            return None
        frozen = policy.get("runtime_profile")
        if not isinstance(frozen, dict) or not frozen.get("profile_name"):
            return None
        context_data = frozen.get("context") or {}
        window_data = frozen.get("window") or {}
        model_data = frozen.get("model") or {}
        return cls(
            profile_name=str(frozen["profile_name"]),
            model=ModelProfile.from_dict(model_data),
            context=ContextPolicy(
                soft_compaction_target_tokens=int(
                    context_data.get("soft_compaction_target_tokens", 0) or 0
                ),
                keep_recent_turns=int(context_data.get("keep_recent_turns", 1) or 1),
                completion_output_cap_tokens=int(
                    context_data.get("completion_output_cap_tokens", 0) or 0
                ),
                cumulative_token_budget=int(context_data.get("cumulative_token_budget", 0) or 0),
                capacity_margin_tokens=int(context_data.get("capacity_margin_tokens", 0) or 0),
            ),
            window=WindowResolution(
                target_tokens=int(window_data.get("target_tokens", 0) or 0),
                adopted_tokens=int(window_data.get("adopted_tokens", 0) or 0),
                clamped_by_capacity=bool(window_data.get("clamped_by_capacity", False)),
                capacity_tokens=int(window_data.get("capacity_tokens", 0) or 0),
                capacity_checked=bool(window_data.get("capacity_checked", False)),
                capacity_ceiling_tokens=int(window_data.get("capacity_ceiling_tokens", 0) or 0),
            ),
        )
