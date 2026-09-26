"""任务日志上下文:task_id/request_id 经 contextvar 注入日志记录。

P3-9 整改:AGENTS「业务日志必须带 task_id 与 request_id」此前只有约定、
没有装配、没有输出(grep basicConfig/Handler 全 0 命中)。本模块提供
TaskContextFilter(往 LogRecord 上补 task_id/request_id 字段)与
set_task_context/reset_task_context(service._execute 在工作线程首行设置);
装配点在 app.api.app._setup_logging。

如实声明:per-event request_id 的结构缺口(轨迹事件每条新 UUID,与 HTTP
请求/LLM 调用无关联)不在本卡范围——那是另一张重构卡,本模块只提供装配面。
"""

from __future__ import annotations

import contextvars
import logging

task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("patchpilot_task_id", default="")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patchpilot_request_id", default=""
)

_ContextTokens = tuple["contextvars.Token[str]", "contextvars.Token[str]"]


def set_task_context(task_id: str, request_id: str = "") -> _ContextTokens:
    """设置当前线程的任务日志上下文;返回 token 对,供 finally 里 reset。

    ThreadPoolExecutor 的线程跨任务复用,不 reset 会把上一个 task_id
    泄漏进下一个任务的日志。
    """
    return (task_id_var.set(task_id), request_id_var.set(request_id))


def reset_task_context(tokens: _ContextTokens) -> None:
    task_token, request_token = tokens
    task_id_var.reset(task_token)
    request_id_var.reset(request_token)


class TaskContextFilter(logging.Filter):
    """把 contextvar 里的 task_id/request_id 注入每条 LogRecord。

    未设置上下文时输出空串(启动期/非任务日志),不猜值、不回退全局态。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = task_id_var.get()
        record.request_id = request_id_var.get()
        return True
