"""任务级资源账本(S02/F1,ADR-0009 §2)。

缺陷现场(review F1,P1):plain/graph 都只在发请求前检查预算,最后一条回复
(含 finish)的真实 usage 入账后循环直接返回,finish 节点无条件写 resolved——
README 承诺"未超预算才 resolved",终局却没有复查。mini-SWE-agent 允许单次调用
跨阈值,但**超支必须如实入账且终局不得再宣称 resolved**。

账本规格(ADR-0009):
- 一个任务只持有一份线程安全账本;graph 与 plain 共用同一套最终验收读它;
- 每次模型调用有持久 call_id:请求前记 pending,收到回复**立即**先记真实用量,
  再处理工具或 finish;重复同 call_id 入账无效;
- usage 来源标 provider|estimated|unknown;估算不冒充 provider 真值;
- 回复导致任务超限:记录实际 overrun,停止本回复后续工具调用与新模型请求,
  结构化收尾(BudgetError);之前已完成的验证可以记录 passed,严格 verdict 仍失败;
- 崩溃/异常留下 pending 调用且无实际 usage:标 unknown,保全现场,不把余量重授、
  已用不写 0;本轮不做 provider 账单对账;
- 阶段份额耗尽是阶段收敛信号(仍走既有 token_budget 参数),不是任务级超限;
  两者在账本上分开:exhausted=估算闸拦下(实际未超),exceeded=实际用量超限。

G03 在同一份账本上加的是"预留"而不是第二套计数器(SPEC §5、§7 G03):
- **reserve → settle / unknown** 在一个临界区里完成"已用 + 全部在途预留 + 本次预留"
  对每个启用维度的检查。缺陷原形:两个并发请求各自都装得下、合计装不下时,请求前的
  检查都放行(它只看已用),两次都发出去 —— 现金/额度上限形同一半;
- 未知消耗**保留预留**:在途调用崩掉时按预留量继续占额度,恢复只认这份持有量,
  不会因为"账本上没数"把已经花掉(但数不清)的额度再授一次;
- 维度补齐:tokens 之外新增 cost(micro-USD 整数、带价目版本与来源)、调用数、
  单请求容量(声明容量 − 输出预留 − 余量,越过即在请求前拒绝);
- 账本可由调用明细重算(`recomputed()`),并可整体落/取原子文件快照
  (`run_dir/ledger_state.json`),恢复走同一条口径。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.errors import BudgetError
from app.llm.usage import UsageEnvelope

log = logging.getLogger(__name__)

USAGE_PROVIDER = "provider"
USAGE_ESTIMATED = "estimated"
USAGE_UNKNOWN = "unknown"

# resource_status 的取值(spec §3.1);exhausted=额度用尽停止新调用(实际未超),
# exceeded=实际用量越过限额,unknown=存在无法确知用量的在途请求。
WITHIN_BUDGET = "within_budget"
EXHAUSTED = "exhausted"
EXCEEDED = "exceeded"
UNKNOWN = "unknown"

# 预算模式(§5):必须显式冻结,legacy_v1 的 token 闸在 cost/hybrid 下也**不取消**
BUDGET_TOKENS = "tokens"
BUDGET_COST = "cost"
BUDGET_HYBRID = "hybrid"
BUDGET_MODES = (BUDGET_TOKENS, BUDGET_COST, BUDGET_HYBRID)

RESERVATION_RESERVED = "reserved"
RESERVATION_SETTLED = "settled"
RESERVATION_UNKNOWN = "unknown"
RESERVATION_STATES = (RESERVATION_RESERVED, RESERVATION_SETTLED, RESERVATION_UNKNOWN)

LEDGER_STATE_VERSION = 1
LEDGER_STATE_FILENAME = "ledger_state.json"


@dataclass
class CallRecord:
    """一次模型调用的入账记录;status: pending → recorded | unknown。"""

    call_id: str
    stage: str
    round_no: int
    status: str = "pending"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_source: str = USAGE_PROVIDER
    cost_micro_usd: int = 0
    cost_basis: str = ""


@dataclass
class Overrun:
    """一次回复把任务实际用量推过限额的记录(F1 的核心证据)。"""

    call_id: str
    stage: str
    tokens_used: int
    token_limit: int

    def __str__(self) -> str:
        return (
            f"task tokens {self.tokens_used} exceed budget {self.token_limit}"
            f" (call {self.call_id} in {self.stage})"
        )


@dataclass
class CostOverrun:
    """现金维度越过限额的记录(与 token 超支并列,不互相替代)。"""

    call_id: str
    stage: str
    cost_micro_usd: int
    cost_limit_micro_usd: int
    currency: str

    def __str__(self) -> str:
        return (
            f"task cost {self.cost_micro_usd} micro-{self.currency} exceeds limit"
            f" {self.cost_limit_micro_usd} (call {self.call_id} in {self.stage})"
        )


@dataclass
class Reservation:
    """一次在途请求的**预留**:它占额度,哪怕它的花费最后无从知道。"""

    reservation_id: str
    call_id: str
    stage: str
    round_no: int
    request_tokens: int  # 待发上下文(含 schema/reasoning)+ 输出预留
    cost_micro_usd: int | None = None  # 按价目保守折算;无价目时 None
    state: str = RESERVATION_RESERVED
    created_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceLedger:
    """线程安全的任务级用量账本;快照供验收与报告读取。"""

    task_id: str
    token_limit: int  # 0 = 任务级不限制(既有约定保留)
    output_reserve: int = 0  # 为单次回复预留的输出额度;0=无法预留(记录能力边界)
    calls: dict[str, CallRecord] = field(default_factory=dict)
    tokens_used: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    model_calls: int = 0
    stage_usage: dict[str, int] = field(default_factory=dict)
    resource_status: str = WITHIN_BUDGET
    stop_reason: str | None = None
    overrun: Overrun | None = None
    output_reserve_unavailable: bool = False
    # ---------- G03:预算模式与其他维度 ----------
    budget_mode: str = BUDGET_TOKENS
    cost_limit_micro_usd: int = 0  # 0 = 现金维度不限制(tokens 模式下的既有形状)
    call_limit: int = 0  # 模型调用数上限;0 = 不限制
    model_capacity_tokens: int = 0  # 声明的单请求硬容量;0 = 未声明(不检查,如实标注)
    capacity_margin_tokens: int = 0  # 容量余量(协议开销、估算误差)
    price_model: str = ""  # 折价用的模型名;空 = 无价目(cost/hybrid 模式下应被拒绝受理)
    cost_micro_usd: int = 0  # 已结算费用累计
    cost_by_basis: dict[str, int] = field(default_factory=dict)
    price_provenance: dict[str, str] = field(default_factory=dict)
    unknown_held_tokens: int = 0  # 崩掉的在途调用**继续占着**的额度
    unknown_held_cost_micro_usd: int = 0
    reservations: dict[str, Reservation] = field(default_factory=dict)
    settled_reservations: list[dict[str, Any]] = field(default_factory=list)
    unknown_reservations: list[dict[str, Any]] = field(default_factory=list)
    cost_overrun: CostOverrun | None = None
    anomalies: list[str] = field(default_factory=list)
    over_capacity_requests: int = 0  # 因"单请求超出声明容量"被请求前拒绝的次数
    state_path: Path | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    # ---------- 请求前 ----------

    def begin_call(self, *, stage: str, round_no: int = 0) -> str:
        """登记一次在途调用,返回持久 call_id。"""
        call_id = uuid.uuid4().hex
        with self._lock:
            self.calls[call_id] = CallRecord(call_id=call_id, stage=stage, round_no=round_no)
        return call_id

    def _tokens_gated(self) -> bool:
        return self.budget_mode in (BUDGET_TOKENS, BUDGET_HYBRID)

    def _cost_gated(self) -> bool:
        return self.budget_mode in (BUDGET_COST, BUDGET_HYBRID)

    def _projected_cost(self, *, request_tokens: int) -> int | None:
        """保守预留折算:未缓存输入价 × 待发量 + 输出价 × 输出预留(§5)。

        没有价目就返回 None —— cost/hybrid 模式下这是"无价格可算",由受理处的
        `validate_budget_policy` 拒绝;运行到这里只可能是 tokens 模式,不虚构金额。
        """
        if not self._cost_gated() or not self.price_model:
            return None
        from app.evals.pricing import price_card_for

        card = price_card_for(self.price_model)
        if card is None:
            return None
        uncached = max(int(request_tokens) - max(self.output_reserve, 0), 0)
        cost = card.cost_micro_usd(
            prompt_tokens=uncached, completion_tokens=max(self.output_reserve, 0)
        )
        self.price_provenance = {
            "currency": card.currency,
            "price_version": card.version,
            "price_source": card.source,
        }
        return cost

    def _in_flight(self) -> list[Reservation]:
        return [r for r in self.reservations.values() if r.state == RESERVATION_RESERVED]

    def _reserved_tokens(self) -> int:
        return sum(r.request_tokens for r in self._in_flight())

    def _reserved_cost(self) -> int:
        return sum(int(r.cost_micro_usd or 0) for r in self._in_flight())

    @property
    def committed_tokens(self) -> int:
        """已用 + 未知持有 + 全部在途预留:这是"还能不能再发一次"的比较基数。"""
        with self._lock:
            return self.tokens_used + self.unknown_held_tokens + self._reserved_tokens()

    @property
    def committed_cost_micro_usd(self) -> int:
        with self._lock:
            return self.cost_micro_usd + self.unknown_held_cost_micro_usd + self._reserved_cost()

    def _check_room_locked(self, *, tokens: int, cost: int | None, stage: str) -> None:
        """额度检查(必须在锁内调用)。已用 + 在途预留 + 本次预留 才是完整基数。"""
        if self._tokens_gated() and self.token_limit > 0:
            committed = self.tokens_used + self.unknown_held_tokens + self._reserved_tokens()
            if committed + tokens > self.token_limit:
                if self.resource_status == WITHIN_BUDGET:
                    self.resource_status = EXHAUSTED
                    self.stop_reason = (
                        f"task token budget {self.token_limit} exhausted before request:"
                        f" used {self.tokens_used} + in flight {self._reserved_tokens()}"
                        f" + unknown held {self.unknown_held_tokens} + pending {tokens}"
                    )
                raise BudgetError(self.stop_reason or "task token budget exhausted")
        if self._cost_gated() and self.cost_limit_micro_usd > 0:
            if cost is None:
                # 要按现金闸却算不出价目:不是"免检",而是这次不能发(受理处已应拒绝)
                self.anomalies.append(f"cost_mode_request_unpriced:{stage}")
                raise BudgetError(
                    f"budget_mode={self.budget_mode} requires a known price for"
                    f" '{self.price_model}'; refusing request in {stage}"
                )
            committed = (
                self.cost_micro_usd + self.unknown_held_cost_micro_usd + self._reserved_cost()
            )
            if committed + cost > self.cost_limit_micro_usd:
                if self.resource_status == WITHIN_BUDGET:
                    self.resource_status = EXHAUSTED
                    self.stop_reason = (
                        f"task cost budget {self.cost_limit_micro_usd} micro-USD exhausted"
                        f" before request: settled {self.cost_micro_usd} + in flight"
                        f" {self._reserved_cost()} + unknown held"
                        f" {self.unknown_held_cost_micro_usd} + pending {cost}"
                    )
                raise BudgetError(self.stop_reason or "task cost budget exhausted")

    def reserve(
        self,
        *,
        call_id: str,
        stage: str,
        round_no: int = 0,
        request_tokens: int,
        output_reserve: int | None = None,
    ) -> Reservation:
        """原子预留:检查与登记在**同一个临界区**里完成(G03 的并发竞态点)。

        `request_tokens` 是待发请求的完整容量估算(含有效 schema、协议开销、需回传的
        reasoning;见 app/llm/usage.py 的 RequestEstimate),它与额度闸那个读数分开:
        后者的算法被 M15/M17 的历史锚点钉住,不许回溯改动。
        """
        reserve_out = self.output_reserve if output_reserve is None else int(output_reserve)
        tokens = max(int(request_tokens), 0) + max(reserve_out, 0)
        cost = self._projected_cost(request_tokens=tokens)
        with self._lock:
            if self.call_limit > 0:
                in_flight = len(self._in_flight())
                if self.model_calls + in_flight >= self.call_limit:
                    raise BudgetError(
                        f"task model-call limit {self.call_limit} reached:"
                        f" settled {self.model_calls} + in flight {in_flight} (stage {stage})"
                    )
            if self.model_capacity_tokens > 0:
                ceiling = self.model_capacity_tokens - max(self.capacity_margin_tokens, 0)
                if tokens > ceiling:
                    # 越过声明容量:请求前拒绝。这不是"额度用尽"(总额还有余量),
                    # 是这一发请求本身就装不下 —— 状态留给调用方判读,计数如实留下
                    self.over_capacity_requests += 1
                    raise BudgetError(
                        f"request of {tokens} tokens exceeds declared model capacity"
                        f" {self.model_capacity_tokens} (margin {self.capacity_margin_tokens})"
                        f" in stage {stage}"
                    )
            if self.output_reserve <= 0 and reserve_out <= 0:
                self.output_reserve_unavailable = True
            self._check_room_locked(tokens=tokens, cost=cost, stage=stage)
            reservation = Reservation(
                reservation_id=uuid.uuid4().hex,
                call_id=call_id,
                stage=stage,
                round_no=round_no,
                request_tokens=tokens,
                cost_micro_usd=cost,
                created_at=_now_iso(),
            )
            self.reservations[reservation.reservation_id] = reservation
            self._persist_locked()
            return reservation

    def ensure_request_fits(self, pending_context_tokens: int) -> None:
        """请求前的任务级估算检查(含单次输出预留)。

        与阶段份额检查(plain_loop 的 token_budget 参数)分开:这里拦截的是
        **任务总额**,超了就是停止新模型调用的信号。实际未超 → 状态记 exhausted。
        output_reserve<=0 表示输出上限未配置/为 0:无法预留,如实记录能力边界,
        不宣称"绝不超账单"(mini-SWE-agent 同款诚实口径)。
        """
        if self.token_limit <= 0:
            return
        with self._lock:
            if self.output_reserve <= 0:
                self.output_reserve_unavailable = True
            self._check_room_locked(
                tokens=int(pending_context_tokens) + max(self.output_reserve, 0),
                cost=None,
                stage="-",
            )

    # ---------- 回复后(先入账,再处理工具/finish) ----------

    def record_usage(
        self,
        call_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        source: str = USAGE_PROVIDER,
        usage: Any | None = None,
    ) -> Overrun | None:
        """把一次回复的 usage 入账(预留就此结算);返回 Overrun 表示任务已实际超限(F1)。

        total_tokens 供"异常携带可知消耗"的入账路径使用(预算异常只有总额,
        无输入/输出明细);source 标 provider|estimated,估算不冒充真值。
        幂等:同 call_id 第二次入账无效(不按整份 messages 重复收费)。
        `usage` 传 UsageEnvelope 时费用按缓存分档折(micro-USD 整数累计);
        没传就按这三个 int 折,来源标签决定它算"估算费用"还是"端点用量折的费用"。
        """
        with self._lock:
            record = self.calls.get(call_id)
            if record is None or record.status != "pending":
                if record is not None and record.status == "recorded":
                    # 重复结算:第二次必须无效,并把这件事本身留下(幂等性的证据)
                    self.anomalies.append(f"duplicate_settle_ignored:{call_id[:8]}")
                return None  # 重复/未知 call_id:无效,不入账
            record.status = "recorded"
            record.prompt_tokens = max(prompt_tokens, 0)
            record.completion_tokens = max(completion_tokens, 0)
            record.total_tokens = (
                max(total_tokens, 0)
                if total_tokens is not None
                else record.prompt_tokens + record.completion_tokens
            )
            record.usage_source = source
            self.tokens_prompt += record.prompt_tokens
            self.tokens_completion += record.completion_tokens
            self.tokens_used += record.total_tokens
            self.model_calls += 1
            self.stage_usage[record.stage] = (
                self.stage_usage.get(record.stage, 0) + record.total_tokens
            )
            self._settle_reservation_locked(call_id=call_id)
            self._accrue_cost_locked(record=record, usage=usage)
            if self.token_limit > 0 and self.tokens_used > self.token_limit:
                self.overrun = Overrun(
                    call_id=call_id,
                    stage=record.stage,
                    tokens_used=self.tokens_used,
                    token_limit=self.token_limit,
                )
                self.resource_status = EXCEEDED
                self.stop_reason = str(self.overrun)
                self._persist_locked()
                return self.overrun
            if (
                self._cost_gated()
                and self.cost_limit_micro_usd > 0
                and self.cost_micro_usd + self.unknown_held_cost_micro_usd + self._reserved_cost()
                > self.cost_limit_micro_usd
            ):
                # 实际回复把现金用量推过上限:如实记录并停,不追平到上限伪装合规
                self.cost_overrun = CostOverrun(
                    call_id=call_id,
                    stage=record.stage,
                    cost_micro_usd=self.cost_micro_usd,
                    cost_limit_micro_usd=self.cost_limit_micro_usd,
                    currency=self.price_provenance.get("currency", "USD"),
                )
                self.resource_status = EXCEEDED
                self.stop_reason = str(self.cost_overrun)
                self._persist_locked()
                return None
            self._persist_locked()
            return None

    def _settle_reservation_locked(self, *, call_id: str) -> None:
        for reservation in list(self.reservations.values()):
            if reservation.call_id != call_id:
                continue
            reservation.state = RESERVATION_SETTLED
            reservation.closed_at = _now_iso()
            del self.reservations[reservation.reservation_id]
            self.settled_reservations.append(reservation.as_dict())

    def _accrue_cost_locked(self, *, record: CallRecord, usage: Any | None) -> None:
        """按冻结价目把这次用量折成 micro-USD 并累计。无价目 = 不虚构金额。"""
        if not self.price_model:
            return
        from app.evals.pricing import cost_of_usage

        envelope = usage
        if envelope is None:
            envelope = UsageEnvelope(
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                usage_source=record.usage_source,
            )
        cost = cost_of_usage(self.price_model, envelope)
        if cost is None:
            self.anomalies.append(f"price_unavailable:{self.price_model}")
            return
        record.cost_micro_usd = cost.micro_usd
        record.cost_basis = cost.basis
        self.cost_micro_usd += cost.micro_usd
        self.cost_by_basis[cost.basis] = self.cost_by_basis.get(cost.basis, 0) + cost.micro_usd
        self.price_provenance = {
            "currency": cost.currency,
            "price_version": cost.price_version,
            "price_source": cost.price_source,
            "cache_pricing": cost.cache_pricing,
        }

    def abandon_call(self, call_id: str, *, reason: str) -> None:
        """在途调用被打断且拿不到真实 usage:标 unknown,并**继续占着预留额度**。

        不写 0、不重授:这笔花费无从知道,但它确实可能发生过了。恢复侧读到的必须是
        同一个持有量,否则一次崩溃就把已花的额度还回来了(那是本卡要堵的原洞)。
        """
        with self._lock:
            record = self.calls.get(call_id)
            if record is None or record.status != "pending":
                return
            record.status = "unknown"
            record.usage_source = USAGE_UNKNOWN
            held = self._release_reservation_locked(call_id=call_id)
            self.unknown_held_tokens += held[0]
            self.unknown_held_cost_micro_usd += held[1]
            self.resource_status = UNKNOWN
            self.stop_reason = (
                f"model call {call_id} abandoned without usage: {reason}"
                f" (held {held[0]} tokens"
                f"{f' / {held[1]} micro-USD' if held[1] else ''} as unknown)"
            )
            self._persist_locked()

    def _release_reservation_locked(self, *, call_id: str) -> tuple[int, int]:
        """把在途预留转成未知持有,返回它占的 (tokens, micro-USD)。"""
        for reservation in list(self.reservations.values()):
            if reservation.call_id != call_id:
                continue
            reservation.state = RESERVATION_UNKNOWN
            reservation.closed_at = _now_iso()
            del self.reservations[reservation.reservation_id]
            self.unknown_reservations.append(reservation.as_dict())
            return (
                int(reservation.request_tokens),
                int(reservation.cost_micro_usd or 0),
            )
        return (0, 0)

    # ---------- 读取 ----------

    def _limits(self) -> dict[str, Any]:
        return {
            "budget_mode": self.budget_mode,
            "token_limit": self.token_limit,
            "cost_limit_micro_usd": self.cost_limit_micro_usd,
            "call_limit": self.call_limit,
            "model_capacity_tokens": self.model_capacity_tokens,
            "capacity_margin_tokens": self.capacity_margin_tokens,
            "output_reserve": self.output_reserve,
        }

    def recomputed(self) -> dict[str, Any]:
        with self._lock:
            return self._recomputed_locked()

    def _recomputed_locked(self) -> dict[str, Any]:
        """从调用明细重算累计量 —— 主账本必须可由明细复算(G03 的退出条件)。"""
        recorded = [c for c in self.calls.values() if c.status == "recorded"]
        stage_usage: dict[str, int] = {}
        for record in recorded:
            stage_usage[record.stage] = stage_usage.get(record.stage, 0) + record.total_tokens
        return {
            "tokens_used": sum(c.total_tokens for c in recorded),
            "tokens_prompt": sum(c.prompt_tokens for c in recorded),
            "tokens_completion": sum(c.completion_tokens for c in recorded),
            "model_calls": len(recorded),
            "cost_micro_usd": sum(c.cost_micro_usd for c in recorded),
            "stage_usage": stage_usage,
        }

    def is_consistent(self) -> bool:
        """累计量与明细重算是否一致(报告与测试读它,不接受"大概是对的")。"""
        with self._lock:
            live = {
                "tokens_used": self.tokens_used,
                "tokens_prompt": self.tokens_prompt,
                "tokens_completion": self.tokens_completion,
                "model_calls": self.model_calls,
                "cost_micro_usd": self.cost_micro_usd,
                "stage_usage": dict(self.stage_usage),
            }
            return live == self._recomputed_locked()

    def snapshot(self) -> dict:
        """原子快照(验收与 report 用);存储失败按证据不完整收敛,不再收费。"""
        with self._lock:
            return {
                "task_id": self.task_id,
                "token_limit": self.token_limit,
                "tokens_used": self.tokens_used,
                "tokens_prompt": self.tokens_prompt,
                "tokens_completion": self.tokens_completion,
                "model_calls": self.model_calls,
                "stage_usage": dict(self.stage_usage),
                "resource_status": self.resource_status,
                "stop_reason": self.stop_reason,
                "overrun": asdict(self.overrun) if self.overrun is not None else None,
                "output_reserve": self.output_reserve,
                "output_reserve_unavailable": self.output_reserve_unavailable,
                # G03 新增维度:上限、已用、在途预留与未知持有分开列,报告里不许混成一个数
                "limits": self._limits(),
                "cost_micro_usd": self.cost_micro_usd,
                "cost_by_basis": dict(self.cost_by_basis),
                "cost_overrun": (
                    asdict(self.cost_overrun) if self.cost_overrun is not None else None
                ),
                "price_provenance": dict(self.price_provenance),
                "unknown_held_tokens": self.unknown_held_tokens,
                "unknown_held_cost_micro_usd": self.unknown_held_cost_micro_usd,
                "reserved_tokens": self._reserved_tokens(),
                "reserved_cost_micro_usd": self._reserved_cost(),
                "committed_tokens": (
                    self.tokens_used + self.unknown_held_tokens + self._reserved_tokens()
                ),
                "committed_cost_micro_usd": (
                    self.cost_micro_usd + self.unknown_held_cost_micro_usd + self._reserved_cost()
                ),
                "in_flight": [r.as_dict() for r in self._in_flight()],
                "over_capacity_requests": self.over_capacity_requests,
                "anomalies": list(self.anomalies),
                "recomputed": {
                    k: v
                    for k, v in self._recomputed_locked().items()
                    if k in ("tokens_used", "model_calls", "cost_micro_usd")
                },
                "calls": [
                    {
                        "call_id": c.call_id,
                        "stage": c.stage,
                        "round_no": c.round_no,
                        "status": c.status,
                        "prompt_tokens": c.prompt_tokens,
                        "completion_tokens": c.completion_tokens,
                        "total_tokens": c.total_tokens,
                        "usage_source": c.usage_source,
                        "cost_micro_usd": c.cost_micro_usd,
                        "cost_basis": c.cost_basis,
                    }
                    for c in self.calls.values()
                ],
            }

    # ---------- 持久化(恢复不重授的前提) ----------

    def to_state(self) -> dict[str, Any]:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        """可 checkpoint 的账本状态:明细 + 在途/未知预留 + 上限(调用方必须已持锁)。"""
        return {
            "version": LEDGER_STATE_VERSION,
            "task_id": self.task_id,
            "limits": self._limits(),
            "tokens_used": self.tokens_used,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "model_calls": self.model_calls,
            "stage_usage": dict(self.stage_usage),
            "resource_status": self.resource_status,
            "stop_reason": self.stop_reason,
            "output_reserve_unavailable": self.output_reserve_unavailable,
            "cost_micro_usd": self.cost_micro_usd,
            "cost_by_basis": dict(self.cost_by_basis),
            "price_model": self.price_model,
            "price_provenance": dict(self.price_provenance),
            "unknown_held_tokens": self.unknown_held_tokens,
            "unknown_held_cost_micro_usd": self.unknown_held_cost_micro_usd,
            "over_capacity_requests": self.over_capacity_requests,
            "anomalies": list(self.anomalies),
            "calls": [asdict(c) for c in self.calls.values()],
            "reservations": [r.as_dict() for r in self.reservations.values()],
            "settled_reservations": list(self.settled_reservations),
            "unknown_reservations": list(self.unknown_reservations),
        }

    def _persist_locked(self) -> None:
        """原子写快照(tmp + os.replace)。写失败只告警:账本不能因为落盘而打断运行。"""
        if self.state_path is None:
            return
        payload = self._state_locked()
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as exc:  # 与 loop_state 同一口径:快照缺失只让恢复不可用,不判死任务
            log.warning("task %s: failed to write ledger state (%s)", self.task_id, exc)
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    @classmethod
    def from_state(cls, saved: dict[str, Any]) -> ResourceLedger:
        """由快照重建账本:未知持有照原样带回来,绝不因"账本是新的"重授额度。"""
        limits = dict(saved.get("limits") or {})
        ledger = cls(
            task_id=str(saved.get("task_id", "")),
            token_limit=int(limits.get("token_limit", 0) or 0),
            output_reserve=int(limits.get("output_reserve", 0) or 0),
            budget_mode=str(limits.get("budget_mode", BUDGET_TOKENS)),
            cost_limit_micro_usd=int(limits.get("cost_limit_micro_usd", 0) or 0),
            call_limit=int(limits.get("call_limit", 0) or 0),
            model_capacity_tokens=int(limits.get("model_capacity_tokens", 0) or 0),
            capacity_margin_tokens=int(limits.get("capacity_margin_tokens", 0) or 0),
            price_model=str(saved.get("price_model", "")),
        )
        ledger.tokens_used = int(saved.get("tokens_used", 0) or 0)
        ledger.tokens_prompt = int(saved.get("tokens_prompt", 0) or 0)
        ledger.tokens_completion = int(saved.get("tokens_completion", 0) or 0)
        ledger.model_calls = int(saved.get("model_calls", 0) or 0)
        ledger.stage_usage = dict(saved.get("stage_usage") or {})
        ledger.resource_status = str(saved.get("resource_status", WITHIN_BUDGET))
        ledger.stop_reason = saved.get("stop_reason")
        ledger.output_reserve_unavailable = bool(saved.get("output_reserve_unavailable"))
        ledger.cost_micro_usd = int(saved.get("cost_micro_usd", 0) or 0)
        ledger.cost_by_basis = dict(saved.get("cost_by_basis") or {})
        ledger.price_provenance = dict(saved.get("price_provenance") or {})
        ledger.unknown_held_tokens = int(saved.get("unknown_held_tokens", 0) or 0)
        ledger.unknown_held_cost_micro_usd = int(saved.get("unknown_held_cost_micro_usd", 0) or 0)
        ledger.over_capacity_requests = int(saved.get("over_capacity_requests", 0) or 0)
        ledger.anomalies = list(saved.get("anomalies") or [])
        ledger.calls = {
            str(c["call_id"]): CallRecord(
                **{k: v for k, v in c.items() if k in CallRecord.__dataclass_fields__}
            )
            for c in saved.get("calls") or []
        }
        ledger.reservations = {
            str(r["reservation_id"]): Reservation(
                **{k: v for k, v in r.items() if k in Reservation.__dataclass_fields__}
            )
            for r in saved.get("reservations") or []
        }
        ledger.settled_reservations = list(saved.get("settled_reservations") or [])
        ledger.unknown_reservations = list(saved.get("unknown_reservations") or [])
        return ledger


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def ledger_state_path(run_dir: Path) -> Path:
    return Path(run_dir) / LEDGER_STATE_FILENAME


def report_summary(ledger: ResourceLedger | None) -> dict[str, Any]:
    """报告用的账本摘要:判定要的每个维度都在,密钥/思考原文/任意路径一个都不进(T24)。

    明细级 `calls` 不进报告(它已经在 `ledger_state.json` 与轨迹里,报告只需要能核对
    "累计数与明细对得上"这一件事 → `calls_consistent`)。
    """
    if ledger is None:
        return {"resource_status": UNKNOWN}
    snap = ledger.snapshot()
    summary = {
        key: snap[key]
        for key in (
            "limits",
            "tokens_used",
            "tokens_prompt",
            "tokens_completion",
            "model_calls",
            "stage_usage",
            "resource_status",
            "stop_reason",
            "overrun",
            "cost_micro_usd",
            "cost_by_basis",
            "cost_overrun",
            "price_provenance",
            "unknown_held_tokens",
            "unknown_held_cost_micro_usd",
            "reserved_tokens",
            "reserved_cost_micro_usd",
            "committed_tokens",
            "committed_cost_micro_usd",
            "over_capacity_requests",
            "anomalies",
            "output_reserve_unavailable",
            "recomputed",
        )
    }
    summary["calls_consistent"] = ledger.is_consistent()
    summary["budget_mode"] = snap["limits"]["budget_mode"]
    return summary


def ledger_from_settings(
    *,
    task_id: str,
    settings: Any,
    price_model: str = "",
    run_dir: Path | None = None,
) -> ResourceLedger:
    """两引擎共用的唯一构造口径:上限全部来自冻结配置,0 = 不限制。

    `run_dir` 里已有 `ledger_state.json` 时按快照重建 —— 这就是"恢复不重授"的执行点:
    账本不再是空的一份,未知持有与在途预留照原样带回来,上限也沿用**受理时冻结的那份**
    (环境变量后来被改大也不给已经跑到一半的任务加额度)。
    """
    state_path = ledger_state_path(run_dir) if run_dir is not None else None
    if state_path is not None:
        saved = load_ledger_state(state_path.parent)
        if saved is not None:
            ledger = ResourceLedger.from_state(saved)
            ledger.state_path = state_path
            if price_model and not ledger.price_model:
                ledger.price_model = price_model
            return ledger
    return ResourceLedger(
        task_id=task_id,
        token_limit=int(getattr(settings, "token_budget", 0) or 0),
        output_reserve=int(getattr(settings, "llm_max_tokens", 0) or 0),
        budget_mode=str(getattr(settings, "budget_mode", None) or BUDGET_TOKENS),
        cost_limit_micro_usd=int(getattr(settings, "cost_budget_micro_usd", 0) or 0),
        call_limit=int(getattr(settings, "max_model_calls", 0) or 0),
        model_capacity_tokens=int(getattr(settings, "model_context_tokens", 0) or 0),
        capacity_margin_tokens=int(getattr(settings, "capacity_margin_tokens", 0) or 0),
        price_model=price_model,
        state_path=state_path,
    )


def load_ledger_state(run_dir: Path) -> dict[str, Any] | None:
    """读账本快照;缺失/损坏/版本不符一律返回 None(与 loop_state 的读侧口径一致)。"""
    path = ledger_state_path(run_dir)
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ledger state %s unreadable: %s", path, exc)
        return None
    if not isinstance(saved, dict) or int(saved.get("version", 0)) != LEDGER_STATE_VERSION:
        log.warning("ledger state %s ignored: version mismatch", path)
        return None
    return saved
