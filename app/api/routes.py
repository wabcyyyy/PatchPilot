"""FastAPI 路由:企划书第 7 节的六个端点 + 统一错误结构。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.api.admission import (
    AdmissionQueueFullError,
    AdmissionTimeoutError,
    EmergencyStopError,
    get_admission_controller,
)
from app.api.auth import (
    ACTION_APPROVE,
    ACTION_CANCEL,
    ACTION_CREATE,
    ACTION_MESSAGE,
    ACTION_READ,
    authorize_task_access,
    get_current_principal,
    require_token,
)
from app.api.events import stream_task_events
from app.api.report import render_markdown
from app.api.schemas import (
    AcceptanceApproveIn,
    AcceptanceOut,
    MessageIn,
    MessageOut,
    QuestionOut,
    TaskCreateIn,
    TaskListOut,
    TaskOut,
    TrajectoryPage,
)
from app.config import get_settings
from app.errors import InvalidRequestError, PatchPilotError, QuotaExceededError, TaskError
from app.observability import MetricsRegistry, check_liveness, check_readiness
from app.security.data_policy import redact_data

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _service(request: Request) -> Any:
    return getattr(request.app.state, "service", None)


def _error(status: int, code: str, message: str, task_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "task_id": task_id}
    )


@router.post("/tasks")
def create_task(payload: TaskCreateIn, request: Request):
    principal = get_current_principal(request)
    owner = str(principal.get("id") or "legacy-unowned")
    admission_ctrl = getattr(request.app.state, "admission_controller", None)
    if admission_ctrl is None:
        admission_ctrl = get_admission_controller()

    try:
        with admission_ctrl.acquire_permit():
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
                agent_profile=payload.agent_profile,
                arm=payload.arm,
                owner=owner,
                principal_id=owner,
                budget_usd=payload.budget_usd,
                token_budget=payload.token_budget,
            )
            if created and hasattr(_service(request).repo, "record_audit_event"):
                p_hash = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
                _service(request).repo.record_audit_event(
                    actor_id=owner,
                    action=ACTION_CREATE,
                    task_id=task.get("id") or task.get("task_id"),
                    target_type="task",
                    target_id=task.get("id") or task.get("task_id"),
                    payload_hash=p_hash,
                    result="success",
                )
    except EmergencyStopError as exc:
        return _error(503, "emergency_stop", str(exc))
    except AdmissionQueueFullError as exc:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "5"},
            content={"code": "queue_full", "message": str(exc), "task_id": None},
        )
    except AdmissionTimeoutError as exc:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "2"},
            content={"code": "admission_timeout", "message": str(exc), "task_id": None},
        )
    except QuotaExceededError as exc:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "60"},
            content={"code": "quota_exceeded", "message": str(exc), "task_id": None},
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
    return JSONResponse(
        status_code=201 if created else 200,
        content=TaskOut(**redact_data(task)).model_dump(),
    )


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(request: Request, limit: int = 50):
    # N-19 整改:SQLite 对 LIMIT -1 语义为"不限",负数 limit 必须夹到 0
    principal = get_current_principal(request)
    role = str(principal.get("role") or "member")
    effective_limit = max(0, min(limit, 200))
    if role in ("admin", "reviewer"):
        rows = _service(request).list_tasks(limit=effective_limit)
    else:
        principal_id = str(principal.get("id") or "legacy-unowned")
        rows = _service(request).list_tasks(limit=effective_limit, principal_id=principal_id)
    return TaskListOut(tasks=[TaskOut(**redact_data(row)) for row in rows])


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, request: Request):
    principal = get_current_principal(request)
    task = _service(request).get_task(task_id)
    if task is None or not authorize_task_access(
        principal, task, ACTION_READ, repo=_service(request).repo
    ):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    return TaskOut(**redact_data(task))


@router.get("/tasks/{task_id}/trajectory")
def get_trajectory(task_id: str, request: Request, limit: int = 200, offset: int = 0):
    service = _service(request)
    principal = get_current_principal(request)
    task = service.get_task(task_id)
    if task is None or not authorize_task_access(principal, task, ACTION_READ, repo=service.repo):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    events = service.trajectory(task_id, limit=max(0, min(limit, 1000)), offset=max(0, offset))
    scrubbed_events = redact_data(events)
    return TrajectoryPage(
        task_id=task_id, total_returned=len(scrubbed_events), offset=offset, events=scrubbed_events
    )


@router.get("/tasks/{task_id}/report")
def get_report(task_id: str, request: Request, format: str = "json"):
    service = _service(request)
    principal = get_current_principal(request)
    task = service.get_task(task_id)
    if task is None or not authorize_task_access(principal, task, ACTION_READ, repo=service.repo):
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
    result = redact_data(result)
    if format == "markdown":
        return PlainTextResponse(render_markdown(result), media_type="text/markdown; charset=utf-8")
    return JSONResponse(content=result)


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: str, request: Request):
    principal = get_current_principal(request)
    task = _service(request).get_task(task_id)
    if task is None or not authorize_task_access(
        principal, task, ACTION_CANCEL, repo=_service(request).repo
    ):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    try:
        cancelled = _service(request).cancel_task(task_id)
        if hasattr(_service(request).repo, "record_audit_event"):
            _service(request).repo.record_audit_event(
                actor_id=str(principal.get("id") or ""),
                action=ACTION_CANCEL,
                task_id=task_id,
                target_type="task",
                target_id=task_id,
                result="success",
            )
        return TaskOut(**redact_data(cancelled))
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc))
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.post("/tasks/{task_id}/acceptance", response_model=AcceptanceOut)
def approve_acceptance(task_id: str, payload: AcceptanceApproveIn, request: Request):
    """G10:trusted_user 批准 diagnose_then_repair 任务的验收清单(冻结 manifest)。"""
    principal = get_current_principal(request)
    task = _service(request).get_task(task_id)
    if task is None or not authorize_task_access(
        principal, task, ACTION_APPROVE, repo=_service(request).repo
    ):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    try:
        actor_id = str(principal.get("id") or "trusted_user")
        result = _service(request).approve_acceptance(
            task_id,
            proposal_id=payload.proposal_id,
            content_hash=payload.content_hash,
            expected_revision=payload.expected_revision,
            approved_by=actor_id,
        )
        if hasattr(_service(request).repo, "record_audit_event"):
            _service(request).repo.record_audit_event(
                actor_id=actor_id,
                action=ACTION_APPROVE,
                task_id=task_id,
                target_type="acceptance_manifest",
                target_id=payload.proposal_id,
                payload_hash=payload.content_hash,
                result="success",
            )
        return AcceptanceOut(**result)
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc), task_id=task_id)
    except InvalidRequestError as exc:
        return _error(422, "invalid_request", str(exc), task_id=task_id)
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/live")
def health_live():
    return JSONResponse(status_code=200, content=check_liveness())


@router.get("/health/ready")
def health_ready(request: Request):
    service = _service(request)
    settings = get_settings()
    repo = getattr(service, "repo", None)
    runs_root = getattr(service, "runs_root", settings.runs_root)
    is_ready, details = check_readiness(
        repo=repo,
        runs_root=runs_root,
        worker_enabled=getattr(settings, "worker_enabled", True),
        min_free_disk_bytes=getattr(settings, "readiness_min_disk_free_bytes", 100 * 1024 * 1024),
    )
    status_code = 200 if is_ready else 503
    return JSONResponse(status_code=status_code, content=details)


@router.get("/metrics")
def metrics(request: Request):
    service = _service(request)
    settings = get_settings()
    repo = getattr(service, "repo", None)
    runs_root = getattr(service, "runs_root", settings.runs_root)
    reg = MetricsRegistry.get_instance()
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return JSONResponse(status_code=200, content=reg.collect(repo, runs_root))
    return PlainTextResponse(
        content=reg.render_prometheus(repo, runs_root),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/capabilities")
def get_capabilities(request: Request):
    """G16:engine/profile/arm/role/model/预算的真实有效值,一个出口可查。

    读数与受理层同源(app/api/capabilities.py 从各登记处现取);客户端在提交前
    就能查到"什么能受理、什么被显式拒绝",而不是靠试错。
    """
    from app.api.capabilities import capabilities
    from app.config import get_settings

    return capabilities(get_settings())


@router.post("/tasks/{task_id}/messages", response_model=MessageOut)
def submit_message(task_id: str, payload: MessageIn, request: Request):
    """G11:回答澄清问题(answer)或追加途中纠偏(steer)。

    与 acceptance 批准同一条纪律:输入只经已配置鉴权的 HTTP 面写入(router 级
    require_token),模型文字与仓库文件都不是输入来源。
    """
    principal = get_current_principal(request)
    task = _service(request).get_task(task_id)
    if task is None or not authorize_task_access(
        principal, task, ACTION_MESSAGE, repo=_service(request).repo
    ):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    try:
        result = _service(request).submit_message(
            task_id,
            kind=payload.kind,
            text=payload.text,
            question_id=payload.question_id,
            expected_revision=payload.expected_revision,
            idem_key=payload.idem_key,
        )
        if hasattr(_service(request).repo, "record_audit_event"):
            _service(request).repo.record_audit_event(
                actor_id=str(principal.get("id") or ""),
                action=ACTION_MESSAGE,
                task_id=task_id,
                target_type="message",
                target_id=payload.question_id or payload.idem_key or None,
                payload_hash=hashlib.sha256(payload.text.encode("utf-8")).hexdigest(),
                result="success",
            )
        return MessageOut(**result)
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc), task_id=task_id)
    except InvalidRequestError as exc:
        return _error(422, "invalid_request", str(exc), task_id=task_id)
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.get("/tasks/{task_id}/question", response_model=QuestionOut)
def get_question(task_id: str, request: Request):
    """G11:当前未回答问题与输入版本(CLI 展示问题/选项;无问题时 question=null)。"""
    principal = get_current_principal(request)
    task = _service(request).get_task(task_id)
    if task is None or not authorize_task_access(
        principal, task, ACTION_READ, repo=_service(request).repo
    ):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    try:
        result = _service(request).pending_question(task_id)
        return QuestionOut(**result)
    except TaskError as exc:
        return _error(404, "invalid_task", str(exc), task_id=task_id)
    except PatchPilotError as exc:
        return _error(409, "conflict", str(exc), task_id=task_id)


@router.get("/tasks/{task_id}/events")
def task_events(task_id: str, request: Request, last_event_id: int = 0):
    """G12:任务事件流(SSE)——阶段/tool_started·finished/候选/等待/usage/终态。

    事实来源与 `GET /tasks/{id}/trajectory` 同一张表(不另记一份进度);续传锚点是
    事件行自增 id:客户端可带 `?last_event_id=N` 或标准的 `Last-Event-ID` 头,
    只收其后的事件。任务已终态且没有未读事件时立即以一条 terminal 状态帧结束。
    """
    service = _service(request)
    principal = get_current_principal(request)
    task = service.get_task(task_id)
    if task is None or not authorize_task_access(principal, task, ACTION_READ, repo=service.repo):
        return _error(404, "invalid_task", f"task not found: {task_id}")
    resume_id = max(int(last_event_id or 0), 0)
    header = request.headers.get("last-event-id") or ""
    if header.strip().isdigit():
        resume_id = max(resume_id, int(header.strip()))
    credential = getattr(request.state, "credential", None)
    cred_id = credential.get("id") if credential else None
    return StreamingResponse(
        stream_task_events(service, task_id, after_id=resume_id, credential_id=cred_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
