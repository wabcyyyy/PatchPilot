"""任务状态定义(企划书 4.1/4.2)。

LangGraph 的 state 必须可序列化(支持 checkpoint),所以
ToolContext / 模型等工作时对象放在节点闭包里,不进 state。
"""

from __future__ import annotations

from typing import TypedDict

# 状态以字面量流转;终态集合的唯一权威在 app/storage/repository.py
# (P2-2 收敛,含 CANCELLED——引擎不产出它,由 service 收敛)。


class TaskState(TypedDict, total=False):
    """图状态:字段即企划书 7.4 中的"修复轮数、失败签名、变更文件、测试结果"。"""

    # 任务输入
    bug_id: str
    issue_text: str
    failed_tests: list[str]
    regression_tests: list[str]
    allowed_paths: list[str] | None
    max_rounds: int

    # 运行时进度
    status: str
    round_no: int
    failure_signature: str
    findings: str
    feedback: str
    gate_violations: list[str]

    # 验证结果
    baseline_failed: int
    baseline_regression_ok: bool
    verify_failed_ok: bool
    verify_regression_ok: bool
    changed_files: list[str]

    # 预算与结论
    turns: int
    tokens_used: int
    tokens_prompt: int
    tokens_completion: int
    error: str | None
    outcome: str  # resolved | failed | needs_review | invalid
    # N-12 整改:rollback 在 reset 前保全的工作区 diff,供 runner 落盘取证
    preserved_diff: str
