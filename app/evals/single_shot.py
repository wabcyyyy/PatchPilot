"""消融对照臂:同模型、同题、同判定,只把"执行反馈 + 跨轮重试"从循环里拿掉。

真实批次 5/5 resolved 只证明链路完整,不证明 agent 循环有净贡献——要区分二者
需要一条更低的对照臂。本臂与默认臂的差别刻意压到最小:

- 保留:平台基线测试、LOCALIZE 阶段的全部只读调查(同一份提示、同一个工具集)、
  apply_patch 的协议/门禁拒绝反馈,以及 VERIFY 与判定规则(与默认臂逐字同一条代码
  路径,由 run_task 的 agent 插槽保证);
- 去掉:PROPOSE 阶段的 run_tests(补丁对不对,模型自己无从知道),
  以及 VERIFY 失败后的回滚重试(单发即终局)。

两臂的 resolved 率之差就是这两处机制的净贡献;token 之差是它的代价。
入口:`python -m app.evals.run_single --bug <ID> --model openai --arm one_shot`。
"""

from __future__ import annotations

import threading

from app.config import get_settings
from app.errors import BudgetError
from app.evals.bugset import BugTask
from app.graph.nodes import READ_TOOLS
from app.graph.plain_loop import LoopOutcome, run_plain_loop
from app.llm.base import Model
from app.prompts import LOCALIZE_PROMPT, ONE_SHOT_PROPOSE_PROMPT
from app.tools.base import ToolContext
from app.tools.registry import FINISH_TOOL

# 单发补丁阶段的工具集 = agent 臂的写阶段去掉 run_tests。
# reset_workspace 一并去掉:没有执行反馈时"回滚重来"没有判据,留着只会掩盖消融点。
ONE_SHOT_TOOLS = ["list_files", "search_code", "read_file", "git_diff", "apply_patch", FINISH_TOOL]

# 补丁阶段只给一次成型的机会(读文件核对上下文 + 提交 + finish 的余量)
_PROPOSE_TURNS = 6


def _budget_for(spent: int) -> int | None:
    """本阶段可用的 token 余量(与 graph 的 N-11 口径一致:任务级共享一份预算)。

    plain 引擎历史上每个循环各拿满 Settings.token_budget;对照臂两阶段是我们自己
    串的,这里按任务级扣减,否则与 agent 臂的 token 对照失真。
    """
    limit = get_settings().token_budget
    if limit <= 0:
        return None
    return max(limit - spent, 1)


def one_shot_agent(
    ctx: ToolContext,
    model: Model,
    bug: BugTask,
    *,
    max_turns: int = 20,
    started_monotonic: float | None = None,
    time_budget_seconds: int = 0,
    cancel_event: threading.Event | None = None,
) -> LoopOutcome:
    """定位 → 一次成型补丁;无执行反馈,无第二轮。

    两个阶段各自开一个全新会话(与 graph 的 LOCALIZE/PROPOSE 同形),
    补丁阶段能看到的东西只有 Bug 描述 + 定位阶段自己写下的结论——
    它拿不到任何"测试跑出来是什么"的信息。
    """
    localize = run_plain_loop(
        ctx,
        model,
        LOCALIZE_PROMPT.format(
            issue_text=bug.issue_text,
            failed_tests="\n".join(f"- {t}" for t in bug.failed_tests),
        ),
        max_turns=max_turns,
        state_label="LOCALIZE",
        allowed_tools=READ_TOOLS,
        started_monotonic=started_monotonic,
        time_budget_seconds=time_budget_seconds,
        token_budget=_budget_for(0),
        cancel_event=cancel_event,
    )

    prompt = ONE_SHOT_PROPOSE_PROMPT.format(
        issue_text=bug.issue_text,
        findings=localize.summary or "(定位阶段未给出结论;请先用只读工具确认根因)",
    )
    try:
        patch = run_plain_loop(
            ctx,
            model,
            prompt,
            max_turns=_PROPOSE_TURNS,
            state_label="PROPOSE_ONE_SHOT",
            allowed_tools=ONE_SHOT_TOOLS,
            started_monotonic=started_monotonic,
            time_budget_seconds=time_budget_seconds,
            token_budget=_budget_for(localize.tokens_used),
            cancel_event=cancel_event,
        )
    except BudgetError as exc:
        # N-11 同源:补丁阶段超预算时,定位阶段已烧的用量必须记回任务级账本,
        # 否则对照臂的 token 代价被系统性低估
        exc.tokens_spent = getattr(exc, "tokens_spent", 0) + localize.tokens_used  # type: ignore[attr-defined]
        exc.tokens_prompt = getattr(exc, "tokens_prompt", 0) + localize.tokens_prompt  # type: ignore[attr-defined]
        exc.tokens_completion = (  # type: ignore[attr-defined]
            getattr(exc, "tokens_completion", 0) + localize.tokens_completion
        )
        exc.turns = getattr(exc, "turns", 0) + localize.turns  # type: ignore[attr-defined]
        raise

    return LoopOutcome(
        success=patch.success,
        summary=patch.summary,
        turns=localize.turns + patch.turns,
        tokens_used=localize.tokens_used + patch.tokens_used,
        patch_applied=patch.patch_applied,
        finish_declared=patch.finish_declared,
        tokens_prompt=localize.tokens_prompt + patch.tokens_prompt,
        tokens_completion=localize.tokens_completion + patch.tokens_completion,
    )
