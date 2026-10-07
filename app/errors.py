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


class InvalidRequestError(TaskError):
    """请求参数自相矛盾(如 fake 模式未带回放脚本):API 层转 422,不建任务。"""

    code = "invalid_request"


class ExecError(PatchPilotError):
    """命令执行层失败(超时、无法启动等)。"""

    code = "exec"


class GateError(PatchPilotError):
    """质量门禁拒绝(PATCH_REJECTED)。"""

    code = "gate"


class BudgetError(PatchPilotError):
    """超过轮数/时间/token 预算(BUDGET_EXCEEDED)。

    具名耗用量字段(N-11/复盘 P1-5):上层把异常翻译成终态时,循环已烧掉的
    token/turns 不蒸发;字段带默认值,不携带用量的抛出点无需逐个赋值。
    last_content 是模型最后一轮的实质文本,供降级路径当暂定结论使用。
    """

    code = "budget"

    def __init__(
        self,
        message: str,
        *,
        tokens_spent: int = 0,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        turns: int = 0,
        last_content: str = "",
    ) -> None:
        super().__init__(message)
        self.tokens_spent = tokens_spent
        self.tokens_prompt = tokens_prompt
        self.tokens_completion = tokens_completion
        self.turns = turns
        self.last_content = last_content


class TaskCancelled(PatchPilotError):
    """任务被协作式取消:在下个 turn 边界生效(正在跑的 pytest/LLM 调用先完成)。"""

    code = "cancelled"
