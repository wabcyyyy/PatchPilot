"""统一异常分层:API 层兜底转统一错误结构 {code, message, task_id}。"""

from __future__ import annotations


class PatchPilotError(Exception):
    """所有 PatchPilot 业务异常的基类。"""

    code: str = "internal"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TaskError(PatchPilotError):
    """仓库、测试命令或任务参数不合法(INVALID_TASK)。"""

    code = "invalid_task"


class PatchError(PatchPilotError):
    """补丁生成/应用失败(PATCH_REJECTED 的底层原因之一)。"""

    code = "patch"


class ExecError(PatchPilotError):
    """命令执行层失败(超时、无法启动等)。"""

    code = "exec"


class GateError(PatchPilotError):
    """质量门禁拒绝(PATCH_REJECTED)。"""

    code = "gate"


class BudgetError(PatchPilotError):
    """超过轮数/时间/token 预算(BUDGET_EXCEEDED)。"""

    code = "budget"


class TaskCancelled(PatchPilotError):
    """任务被协作式取消:在下个 turn 边界生效(正在跑的 pytest/LLM 调用先完成)。"""

    code = "cancelled"
