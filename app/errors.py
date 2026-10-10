"""统一异常分层:API 层兜底转统一错误结构 {code, message, task_id}。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只在类型位引用:异常层不许拉起 llm 包的运行时依赖
    from app.llm.usage import ModelResponseEnvelope


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


class ModelResponseError(TaskError):
    """端点**已经回复**但结构无法解释(SPEC §4 契约 3 / 验收矩阵 T03)。

    此前这类情况抛裸 TaskError:choices 为空、工具 arguments 不是 JSON、被 max_tokens
    截断的补丁,全都在异常抛出那一刻把已返回的 usage 一起丢掉,账本只剩 unknown,
    报告看不出这笔花费。envelope 必须随异常交付——先计量,再谈解析失败。
    """

    code = "model_response"

    def __init__(self, message: str, *, envelope: ModelResponseEnvelope) -> None:
        super().__init__(message)
        self.envelope = envelope


class ModelTransportError(TaskError):
    """物理请求本身失败(连接/超时/端点 5xx):已知消耗与未知笔数一起交付。

    `envelope.usage.unknown_attempts` 记的是"有几次尝试的花费无从知道"——恢复与
    预算侧据此**不重授余量**(G03),这里只负责把事实带出来。
    """

    code = "model_transport"

    def __init__(self, message: str, *, envelope: ModelResponseEnvelope) -> None:
        super().__init__(message)
        self.envelope = envelope
