"""FastAPI 路由:企划书第 7 节的六个端点 + 统一错误结构。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.auth import require_token
from app.api.report import render_markdown
from app.api.schemas import TaskCreateIn, TrajectoryPage
from app.errors import PatchPilotError, TaskError

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
        task = _service(request).create_task(
            bug_id=payload.bug_id,
            engine=payload.engine,
            model=payload.model,
            max_rounds=payload.max_rounds,
        )
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc))
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc))
    return JSONResponse(status_code=201, content=task)


@router.get("/tasks")
def list_tasks(request: Request, limit: int = 50):
    return {"tasks": _service(request).list_tasks(limit=min(limit, 200))}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    task = _service(request).get_task(task_id)
    if task is None:
        return _error(404, "invalid_task", f"task not found: {task_id}")
    return task


@router.get("/tasks/{task_id}/trajectory")
def get_trajectory(task_id: str, request: Request, limit: int = 200, offset: int = 0):
    service = _service(request)
    if service.get_task(task_id) is None:
        return _error(404, "invalid_task", f"task not found: {task_id}")
    events = service.trajectory(task_id, limit=min(limit, 1000), offset=max(0, offset))
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
    result = json.loads(report_path.read_text(encoding="utf-8"))
    if format == "markdown":
        return PlainTextResponse(render_markdown(result), media_type="text/markdown; charset=utf-8")
    return JSONResponse(content=result)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    try:
        return _service(request).cancel_task(task_id)
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc))
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.get("/health")
def health():
    return {"status": "ok"}
