"""FastAPI 应用工厂:lifespan 里完成启动恢复与优雅停机。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.auth import UnauthorizedError
from app.api.recycle import recycle_finished_tasks
from app.api.routes import router
from app.api.service import TaskService
from app.config import Settings, get_settings
from app.logctx import TaskContextFilter, request_id_var
from app.storage.repository import Repository

log = logging.getLogger(__name__)


class RequestContextMiddleware:
    """纯 ASGI 中间件(复盘 P1-8):每个 HTTP 请求生成 request_id 注入 contextvar,
    并回写 X-Request-ID 响应头。create_task 在请求上下文里读取并随任务线程下发
    (service._execute 的 set_task_context),兑现 AGENTS「业务日志必须带
    task_id 与 request_id」的约定。纯 ASGI 而非 BaseHTTPMiddleware:
    后者把下游跑在独立 task 里,contextvar 语义依赖实现细节。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


def _setup_logging(settings: Settings) -> None:
    """进程级日志装配(P3-9):此前 grep basicConfig/dictConfig/Handler 全 0 命中,
    成功任务的业务日志一条都出不来(全是 info/debug,低于 stdlib lastResort 的
    WARNING 门槛),AGENTS 的 task_id/request_id 约定悬空。

    只在 root 尚无 handler 时接线——uvicorn/pytest 已配好自己的日志时
    不去动人家(也保证重复 create_app 是 no-op);空 token 的启动告警
    见 lifespan(诚实声明:Settings 无 host 键,检测不了实际绑定地址,
    只能提示部署条件)。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, settings.log_level.upper(), logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s [task=%(task_id)s req=%(request_id)s]"
        " %(message)s",
    )
    context_filter = TaskContextFilter()
    for handler in root.handlers:
        handler.addFilter(context_filter)


def create_app(
    db_path: Path | str | None = None,
    runs_root: Path | str | None = None,
    bugs_root: Path | str | None = None,
) -> FastAPI:
    settings = get_settings()
    _setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # R3-Q8 裁断可立即实现项:空 token 启动告警。诚实边界:Settings 无 host
        # 键(绑定地址由 uvicorn CLI 决定),此处检测不了"非回环 + 空 token"
        # 的实际组合,只能把部署条件讲清楚——compose 部署必须带 token。
        if not settings.api_token:
            log.warning(
                "PATCHPILOT_API_TOKEN is empty; authentication is disabled."
                " This is only safe for loopback/local single-user deployment"
                " — set a token before binding to a non-loopback interface"
                " (threat-model §2/§5)"
            )
        repo = Repository(db_path or settings.db_path)
        service = TaskService(
            repo=repo,
            runs_root=Path(runs_root) if runs_root else None,
            bugs_root=bugs_root or Path("bugs"),
        )
        service.recover_stale()
        # P3-12:启动 Grace 扫描——回收停机期间到终态的 FINISHED 任务的可弃集
        # (finally 没跑到的场景);grace 截止内的刚结束任务留给人看
        if settings.recycle_finished_workspace:
            try:
                recycle_finished_tasks(repo, settings.recycle_grace_seconds)
            except Exception:  # 回收失败不得阻断服务启动
                log.exception("startup recycle scan failed")
        app.state.service = service
        yield
        service.shutdown()

    app = FastAPI(title="PatchPilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(UnauthorizedError)
    async def unauthorized_handler(request: Request, exc: UnauthorizedError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"code": "unauthorized", "message": str(exc), "task_id": None},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """复盘 P1-8:Pydantic 校验失败同样收敛为统一错误结构(AGENTS 约定),
        不再裸露 FastAPI 原生 {"detail": [...]};字段级原因留在 message,
        task_id 此时不存在,恒 None。"""
        details = (
            "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            or "request validation failed"
        )
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": details, "task_id": None},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底:未预期异常也必须输出统一错误结构(AGENTS:API 层兜底)。

        N-18 整改:对外返回固定文案——异常原文可能含文件路径/SQL 片段等内部信息;
        原文经 log.exception 留在服务端日志里,并以 error id 关联响应与日志。
        """
        error_id = uuid.uuid4().hex[:12]
        log.exception("unhandled error %s on %s %s", error_id, request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal",
                "message": f"internal server error (ref: {error_id})",
                "task_id": None,
            },
        )

    app.include_router(router)
    return app


app = create_app()
