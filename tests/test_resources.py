"""S02/F1:ResourceLedger 单元矩阵(ADR-0009 §2)。

账本是 F1 修复的核心数据结构:每次模型调用持久 call_id、回复先入账、
超限如实记录(不写 0 不重授)、崩溃 pending 标 unknown。
"""

from __future__ import annotations

import threading

import pytest

from app.errors import BudgetError
from app.graph.resources import (
    EXCEEDED,
    EXHAUSTED,
    UNKNOWN,
    WITHIN_BUDGET,
    ResourceLedger,
)


def test_record_usage_is_idempotent_per_call_id() -> None:
    ledger = ResourceLedger(task_id="T", token_limit=1000)
    call = ledger.begin_call(stage="LOCALIZE", round_no=1)
    assert ledger.record_usage(call, prompt_tokens=30, completion_tokens=20) is None
    # 同 call_id 第二次入账无效(不按整份 messages 重复收费)
    assert ledger.record_usage(call, prompt_tokens=999, completion_tokens=999) is None
    assert ledger.tokens_used == 50
    assert ledger.model_calls == 1


def test_unknown_call_id_is_rejected() -> None:
    ledger = ResourceLedger(task_id="T", token_limit=1000)
    assert ledger.record_usage("nope", prompt_tokens=1, completion_tokens=1) is None
    assert ledger.tokens_used == 0 and ledger.model_calls == 0


def test_reply_over_limit_records_actual_overrun() -> None:
    """F1 核心:最后一条回复把任务推过限额——实际超支入账,状态 exceeded。"""
    ledger = ResourceLedger(task_id="T", token_limit=20000)
    call = ledger.begin_call(stage="PROPOSE_PATCH")
    overrun = ledger.record_usage(call, prompt_tokens=0, completion_tokens=50000)
    assert overrun is not None
    assert overrun.tokens_used == 50000 and overrun.token_limit == 20000
    assert ledger.resource_status == EXCEEDED
    assert ledger.tokens_used == 50000  # 真实超支不丢
    assert "exceed budget 20000" in ledger.stop_reason


def test_exactly_at_limit_stays_within() -> None:
    """资源恰等于 limit 的边界:used == limit 不是超支。"""
    ledger = ResourceLedger(task_id="T", token_limit=100)
    call = ledger.begin_call(stage="LOCALIZE")
    assert ledger.record_usage(call, prompt_tokens=40, completion_tokens=60) is None
    assert ledger.resource_status == WITHIN_BUDGET


def test_ensure_request_fits_blocks_task_total_and_marks_exhausted() -> None:
    """请求前总额检查:发不起这次请求 = 停止新模型调用(exhausted,实际未超)。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000, output_reserve=100)
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.record_usage(call, prompt_tokens=850, completion_tokens=50)  # used=900
    with pytest.raises(BudgetError, match="exhausted"):
        ledger.ensure_request_fits(pending_context_tokens=50)  # 900+50+100 > 1000
    assert ledger.resource_status == EXHAUSTED
    assert ledger.tokens_used == 900  # 已用不被清零


def test_zero_limit_means_unlimited() -> None:
    ledger = ResourceLedger(task_id="T", token_limit=0)
    ledger.ensure_request_fits(pending_context_tokens=10**9)
    call = ledger.begin_call(stage="LOCALIZE")
    assert ledger.record_usage(call, prompt_tokens=10**6, completion_tokens=10**6) is None
    assert ledger.resource_status == WITHIN_BUDGET  # 0=无限,既有约定保留


def test_abandoned_pending_call_marks_unknown_without_zeroing() -> None:
    """崩溃留下 pending 且无 usage:标 unknown,不把余量重授、已用不写 0。"""
    ledger = ResourceLedger(task_id="T", token_limit=1000)
    c1 = ledger.begin_call(stage="LOCALIZE")
    ledger.record_usage(c1, prompt_tokens=100, completion_tokens=50)
    c2 = ledger.begin_call(stage="PROPOSE_PATCH")
    ledger.abandon_call(c2, reason="RuntimeError")
    assert ledger.resource_status == UNKNOWN
    assert ledger.tokens_used == 150  # 已知消耗保留
    assert ledger.calls[c2].usage_source == "unknown"
    assert "without usage" in ledger.stop_reason


def test_stage_usage_and_call_count_accumulate() -> None:
    ledger = ResourceLedger(task_id="T", token_limit=0)
    for stage in ("LOCALIZE", "LOCALIZE", "PROPOSE_PATCH"):
        call = ledger.begin_call(stage=stage)
        ledger.record_usage(call, prompt_tokens=10, completion_tokens=5)
    assert ledger.model_calls == 3
    assert ledger.stage_usage == {"LOCALIZE": 30, "PROPOSE_PATCH": 15}


def test_output_reserve_unavailable_is_recorded_not_claimed() -> None:
    """输出上限 0(无法预留)时记录能力边界,不宣称绝不超账单。"""
    ledger = ResourceLedger(task_id="T", token_limit=100, output_reserve=0)
    ledger.ensure_request_fits(pending_context_tokens=10)
    assert ledger.output_reserve_unavailable is True


def test_snapshot_is_atomic_copy() -> None:
    ledger = ResourceLedger(task_id="T", token_limit=10)
    call = ledger.begin_call(stage="LOCALIZE")
    ledger.record_usage(call, prompt_tokens=5, completion_tokens=5)
    snap = ledger.snapshot()
    assert snap["tokens_used"] == 10 and snap["resource_status"] == WITHIN_BUDGET
    assert snap["calls"][0]["call_id"] == call


def test_concurrent_recording_is_thread_safe() -> None:
    """分支候选并行入账:两候选都入账且总量正确(S02 必测:分支两候选都入账)。"""
    ledger = ResourceLedger(task_id="T", token_limit=0)

    def worker(index: int) -> None:
        call = ledger.begin_call(stage=f"BRANCH-{index}")
        ledger.record_usage(call, prompt_tokens=100, completion_tokens=50)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.model_calls == 4
    assert ledger.tokens_used == 600
