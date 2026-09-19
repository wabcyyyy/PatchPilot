"""FastAPI 路由:企划书第 7 节的六个端点 + 统一错误结构。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.auth import require_token
from app.api.report import render_markdown
from app.api.schemas import TaskCreateIn, TaskListOut, TaskOut, TrajectoryPage
from app.errors import InvalidRequestError, PatchPilotError, TaskError

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _service(request: Request) -> Any:
    return request.app.state.service


def _error(status: int, code: str, message: str, task_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "task_id": task_id}
    )


@router.post("/tasks")
def create_task(payload: TaskCreateIn, request: Request):
    try:
        task, created = _service(request).create_task(
            bug_id=payload.bug_id,
            engine=payload.engine,
            model=payload.model,
            max_rounds=payload.max_rounds,
            repo_path=payload.repo_path,
            issue_text=payload.issue_text,
            failed_tests=payload.failed_tests,
            regression_tests=payload.regression_tests,
            allowed_paths=payload.allowed_paths,
            replay_script=payload.replay_script,
        )
    except InvalidRequestError as exc:
        # 请求参数自相矛盾:422(资源不存在仍走 TaskError→404)
        return _error(422, "invalid_request", str(exc))
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc))
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc))
    # 幂等命中(同键任务在途)返回 200,新建返回 201
    # R2 整改:经 TaskOut 收敛响应面,不再外泄 idem_key/repo_path/内部主键
    return JSONResponse(status_code=201 if created else 200, content=TaskOut(**task).model_dump())


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(request: Request, limit: int = 50):
    # N-19 整改:SQLite 对 LIMIT -1 语义为"不限",负数 limit 必须夹到 0
    rows = _service(request).list_tasks(limit=max(0, min(limit, 200)))
    return TaskListOut(tasks=[TaskOut(**row) for row in rows])


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, request: Request):
    task = _service(request).get_task(task_id)
    if task is None:
        return _error(404, "invalid_task", f"task not found: {task_id}")
    return TaskOut(**task)


@router.get("/tasks/{task_id}/trajectory")
def get_trajectory(task_id: str, request: Request, limit: int = 200, offset: int = 0):
    service = _service(request)
    if service.get_task(task_id) is None:
        return _error(404, "invalid_task", f"task not found: {task_id}")
    events = service.trajectory(task_id, limit=max(0, min(limit, 1000)), offset=max(0, offset))
    return TrajectoryPage(task_id=task_id, total_returned=len(events), offset=offset, events=events)


@router.get("/tasks/{task_id}/report")
def get_report(task_id: str, request: Request, format: str = "json"):
    service = _service(request)
    task = service.get_task(task_id)
    if task is None:
        return _error(404, "invalid_task", f"task not found: {task_id}")
    run_dir = Path(task["run_dir"]) if task.get("run_dir") else None
    report_path = run_dir / "report.json" if run_dir else None
    if report_path is None or not report_path.exists():
        return _error(409, "not_ready", "report not generated yet", task_id=task_id)
    try:
        result = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # N-17/R2 整改:文件存在与写完之间的残余竞态(含 Windows 读句柄互斥)
        # 收敛为"未就绪",而不是 500
        return _error(409, "not_ready", "report is being written", task_id=task_id)
    if format == "markdown":
        return PlainTextResponse(render_markdown(result), media_type="text/markdown; charset=utf-8")
    return JSONResponse(content=result)


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: str, request: Request):
    try:
        task = _service(request).cancel_task(task_id)
        return TaskOut(**task)
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc))
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.get("/health")
def health():
    return {"status": "ok"}
