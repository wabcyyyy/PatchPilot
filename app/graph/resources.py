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
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field

from app.errors import BudgetError

USAGE_PROVIDER = "provider"
USAGE_ESTIMATED = "estimated"
USAGE_UNKNOWN = "unknown"

# resource_status 的取值(spec §3.1);exhausted=额度用尽停止新调用(实际未超),
# exceeded=实际用量越过限额,unknown=存在无法确知用量的在途请求。
WITHIN_BUDGET = "within_budget"
EXHAUSTED = "exhausted"
EXCEEDED = "exceeded"
UNKNOWN = "unknown"


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

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    # ---------- 请求前 ----------

    def begin_call(self, *, stage: str, round_no: int = 0) -> str:
        """登记一次在途调用,返回持久 call_id。"""
        call_id = uuid.uuid4().hex
        with self._lock:
            self.calls[call_id] = CallRecord(call_id=call_id, stage=stage, round_no=round_no)
        return call_id

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
            projected = self.tokens_used + pending_context_tokens + max(self.output_reserve, 0)
            if projected > self.token_limit:
                if self.resource_status == WITHIN_BUDGET:
                    self.resource_status = EXHAUSTED
                    self.stop_reason = (
                        f"task token budget {self.token_limit} exhausted before request:"
                        f" used {self.tokens_used} + pending {pending_context_tokens}"
                        f" + reserve {max(self.output_reserve, 0)}"
                    )
                raise BudgetError(self.stop_reason or "task token budget exhausted")

    # ---------- 回复后(先入账,再处理工具/finish) ----------

    def record_usage(
        self,
        call_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
        source: str = USAGE_PROVIDER,
    ) -> Overrun | None:
        """把一次回复的 usage 入账;返回 Overrun 表示任务已实际超限(F1)。

        total_tokens 供"异常携带可知消耗"的入账路径使用(预算异常只有总额,
        无输入/输出明细);source 标 provider|estimated,估算不冒充真值。
        幂等:同 call_id 第二次入账无效(不按整份 messages 重复收费)。
        """
        with self._lock:
            record = self.calls.get(call_id)
            if record is None or record.status != "pending":
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
            if self.token_limit > 0 and self.tokens_used > self.token_limit:
                self.overrun = Overrun(
                    call_id=call_id,
                    stage=record.stage,
                    tokens_used=self.tokens_used,
                    token_limit=self.token_limit,
                )
                self.resource_status = EXCEEDED
                self.stop_reason = str(self.overrun)
                return self.overrun
            return None

    def abandon_call(self, call_id: str, *, reason: str) -> None:
        """在途调用被异常/取消打断且拿不到真实 usage:标 unknown,不写 0 不重授。"""
        with self._lock:
            record = self.calls.get(call_id)
            if record is None or record.status != "pending":
                return
            record.status = "unknown"
            record.usage_source = USAGE_UNKNOWN
            self.resource_status = UNKNOWN
            self.stop_reason = f"model call {call_id} abandoned without usage: {reason}"

    # ---------- 读取 ----------

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
                "calls": [
                    {
                        "call_id": c.call_id,
                        "stage": c.stage,
                        "round_no": c.round_no,
                        "status": c.status,
                        "total_tokens": c.total_tokens,
                        "usage_source": c.usage_source,
                    }
                    for c in self.calls.values()
                ],
            }
