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
    # 反思提示:上一轮反馈的归一化特征串与连续相同轮数(0 = 无失败)
    last_feedback_signatures: list[str]
    repeat_streak: int
    # 自适应分支(卡5):实时计数在 ctx.patch_fail_streak(工具层 apply_patch 写),
    # `_should_branch` 也读 ctx——只有工具层知道每次应用结果。这里的同名字段是
    # 计划书面要求的 state 形态(检查点/轨迹留痕用),**当前无写入方**:要让它真正
    # 进轨迹得改 rollback 的返回载荷,那属于禁区且不在卡5a 规格内,未擅自动。
    # branching_used 置真后本任务不再分支(至多一次),branch_selected 记胜者序号(卡5b 写)。
    patch_fail_streak: int
    branching_used: bool
    branch_selected: int

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
