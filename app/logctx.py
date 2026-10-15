"""任务日志上下文:task_id/request_id 经 contextvar 注入日志记录。

P3-9 整改:AGENTS「业务日志必须带 task_id 与 request_id」此前只有约定、
没有装配、没有输出(grep basicConfig/Handler 全 0 命中)。本模块提供
TaskContextFilter(往 LogRecord 上补 task_id/request_id 字段)与
set_task_context/reset_task_context(service._execute 在工作线程首行设置);
装配点在 app.api.app._setup_logging。

复盘 P1-8 补齐:request_id 由 app.api.app 的 HTTP 中间件按请求生成并写入
request_id_var,service.create_task 在请求上下文捕获后随任务线程下发。
仍属已知缺口:轨迹事件(trajectory.jsonl)粒度的 request_id 关联
(每条事件的归属请求)不在本模块范围。
"""

from __future__ import annotations

import contextvars
import logging

task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("patchpilot_task_id", default="")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patchpilot_request_id", default=""
)
attempt_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patchpilot_attempt_id", default=""
)
worker_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "patchpilot_worker_id", default=""
)
actor_var: contextvars.ContextVar[str] = contextvars.ContextVar("patchpilot_actor", default="")

_ContextTokens = tuple[contextvars.Token[str], ...]


def set_task_context(
    task_id: str,
    request_id: str = "",
    attempt_id: str = "",
    worker_id: str = "",
    actor: str = "",
) -> _ContextTokens:
    """设置当前线程的任务日志上下文;返回 tokens,供 finally 里 reset。

    ThreadPoolExecutor 的线程跨任务复用,不 reset 会把上一个 task_id
    泄漏进下一个任务的日志。
    """
    return (
        task_id_var.set(task_id),
        request_id_var.set(request_id),
        attempt_id_var.set(attempt_id),
        worker_id_var.set(worker_id),
        actor_var.set(actor),
    )


def reset_task_context(tokens: _ContextTokens) -> None:
    vars_list = (task_id_var, request_id_var, attempt_id_var, worker_id_var, actor_var)
    for var, token in zip(vars_list, tokens, strict=False):
        var.reset(token)


class TaskContextFilter(logging.Filter):
    """把 contextvar 里的 task_id/request_id 等注入每条 LogRecord。

    未设置上下文时输出空串(启动期/非任务日志),不猜值、不回退全局态。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = task_id_var.get()
        record.request_id = request_id_var.get()
        record.attempt_id = attempt_id_var.get()
        record.worker_id = worker_id_var.get()
        record.actor = actor_var.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器 (P13)。

    每行输出单个合法 JSON 对象，包含时间戳、日志级别、logger 名、消息正文，
    以及受控的任务上下文 (task_id, request_id, attempt_id, worker_id, actor)。
    """

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import UTC, datetime

        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": getattr(record, "task_id", "") or "",
            "request_id": getattr(record, "request_id", "") or "",
            "attempt_id": getattr(record, "attempt_id", "") or "",
            "worker_id": getattr(record, "worker_id", "") or "",
            "actor": getattr(record, "actor", "") or "",
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)
