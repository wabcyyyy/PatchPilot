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
    # M6 崩溃恢复:基线 commit 必须是**普通 str** 才能进 checkpoint。恢复方要把工作区
    # 复位到基线(reset_workspace 需完整 sha),而轨迹里的 create_workspace 事件只存了
    # 12 位前缀——只有 state 里的这份是权威的。
    baseline_commit: str
    # S02b:任务级墙钟截止时刻(epoch 秒)。执行启动时建立、随 superstep 进 checkpoint,
    # 恢复沿用原值的剩余时间(不重授);None = 不限时(旧约定 0=无限的同义)。必须可序列化。
    deadline_epoch: float | None

    # 运行时进度
    status: str
    round_no: int
    failure_signature: str
    findings: str
    feedback: str
    # PLAN 阶段(M5)的产物:一段"改哪些文件/符号 + 失效机理 + 最小改动意图 + 验证预期"的
    # 计划文本。它是**跨轮持久的工件**(不是某一轮的一次性输出):失败轮回到 plan 节点时
    # 带着上一版计划与失败反馈做"修订",而不是让下一轮 PROPOSE 冷启动重猜。
    # 必须是普通 str——整个 TaskState 要能被 SqliteSaver 序列化(runner 每任务都开检查点)。
    plan: str
    gate_violations: list[str]
    # 反思提示:上一轮反馈的归一化特征串与连续相同轮数(0 = 无失败)
    last_feedback_signatures: list[str]
    repeat_streak: int
    # 自适应分支(卡5):实时计数在 ctx.patch_fail_streak(工具层 apply_patch 写),
    # `_should_branch` 也读 ctx——只有工具层知道每次应用结果。这里的同名字段是
    # 计划书面要求的 state 形态(检查点/轨迹留痕用),由分支合流节点写回
    # (nodes.py 的 patch_fail_streak 返回载荷)。
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
    # S02(spec §3.1):终局验收的三个解释维度,由 finish 节点写回;
    # 旧 status/verdict/outcome 保留,预算超限仍 BUDGET_EXCEEDED/failed
    validation_status: str  # not_run | passed | failed | inconclusive
    gate_status: str  # not_run | passed | rejected | inconclusive
    resource_status: str  # within_budget | exhausted | exceeded | unknown
