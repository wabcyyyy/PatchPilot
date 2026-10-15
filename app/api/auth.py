"""主体认证与凭据生命周期 (P02)。

支持：
- 服务账号/API Key 映射 Principal，基于不可逆 SHA-256 散列验证；
- 凭据轮换 (rotate)、吊销 (revoke) 与过期 (expire)；
- 开发模式 (deployment_mode=development) 保留共享 api_token 与受控匿名；
- 生产模式 (deployment_mode=production) 严禁匿名与共享 dev token；
- GET /api/health 恒豁免；日志绝不记录凭据原文。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Request

from app.config import get_settings

EXEMPT_PATHS = frozenset({"/api/health", "/api/health/live", "/api/health/ready", "/api/metrics"})


class UnauthorizedError(Exception):
    """鉴权失败: 由 app 层 exception_handler 转统一错误结构 (401)。"""


def get_current_principal(request: Request) -> dict[str, Any]:
    """获取当前已认证主体；未认证抛 UnauthorizedError。"""
    principal = getattr(request.state, "principal", None)
    if not principal:
        raise UnauthorizedError("unauthenticated principal")
    return principal


def require_token(request: Request) -> None:
    """Router 级依赖: 校验 API Key / Bearer Token 并注入 request.state.principal。"""
    if request.url.path in EXEMPT_PATHS:
        return

    settings = get_settings()

    # 提取凭据：优先 Authorization: Bearer <key>，兼容 X-API-Key: <key>
    raw_token: str | None = None
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1].strip()
        else:
            raise UnauthorizedError("missing or invalid bearer token")
    elif "X-API-Key" in request.headers:
        raw_token = request.headers.get("X-API-Key", "").strip() or None

    repo = getattr(getattr(request.app.state, "service", None), "repo", None)

    if settings.deployment_mode == "production":
        # 生产模式：严禁匿名，严禁使用共享 dev api_token
        if not raw_token:
            raise UnauthorizedError("authentication required in production mode")
        if settings.api_token and secrets.compare_digest(
            raw_token.encode(), settings.api_token.encode()
        ):
            raise UnauthorizedError("shared dev token forbidden in production mode")
        if not repo:
            raise UnauthorizedError("authentication service unavailable")

        auth_res = repo.authenticate_credential(raw_token)
        if not auth_res:
            raise UnauthorizedError("invalid, expired, or revoked credentials")
        principal, credential = auth_res
        request.state.principal = principal
        request.state.credential = credential
        return

    # 开发模式 (development)
    if raw_token:
        if repo:
            auth_res = repo.authenticate_credential(raw_token)
            if auth_res:
                principal, credential = auth_res
                request.state.principal = principal
                request.state.credential = credential
                return
        if settings.api_token:
            if secrets.compare_digest(raw_token.encode(), settings.api_token.encode()):
                request.state.principal = {
                    "id": "dev-token-user",
                    "name": "Dev Token User",
                    "role": "admin",
                }
                request.state.credential = None
                return
            raise UnauthorizedError("missing or invalid bearer token")
        raise UnauthorizedError("missing or invalid bearer token")

    # 开发模式且未提供凭据
    if settings.api_token:
        raise UnauthorizedError("missing or invalid bearer token")

    request.state.principal = {
        "id": "dev-user",
        "name": "Developer",
        "role": "admin",
    }
    request.state.credential = None


ACTION_CREATE = "task_create"
ACTION_READ = "task_read"
ACTION_CANCEL = "task_cancel"
ACTION_APPROVE = "task_approve"
ACTION_MESSAGE = "task_message"


def authorize_task_access(
    principal: dict[str, Any],
    task: dict[str, Any],
    action: str,
    *,
    repo: Any = None,
    request_id: str | None = None,
) -> bool:
    """资源级授权与可归因审计 (P03)。

    权限矩阵：
    - 主体为任务属主：全部放行；
    - admin：全部放行；跨属主访问自动记录 audit_event；
    - reviewer：放行读 (ACTION_READ) 与验收批准 (ACTION_APPROVE)；禁止操作他人任务；
    - member / service_account：仅限自身任务；跨任务访问一律拒绝；
    - 拒绝时记录审计；返回 False（路由层收敛为 404，不泄露任务存在性）。
    """
    settings = get_settings()
    principal_id = str(principal.get("id") or "")
    role = str(principal.get("role") or "member")
    task_id = str(task.get("id") or task.get("task_id") or "")
    task_owner = str(task.get("owner") or task.get("principal_id") or "legacy-unowned")

    is_owner = (task_owner != "legacy-unowned" and principal_id == task_owner) or (
        task.get("principal_id") not in (None, "legacy-unowned")
        and principal_id == task.get("principal_id")
    )

    # 1. 属主自身访问
    if is_owner:
        return True

    # 2. 开发模式兼容旧数据
    if settings.deployment_mode == "development" and task_owner == "legacy-unowned":
        return True

    # 3. 管理员 (admin) 跨属主访问
    if role == "admin":
        if repo and hasattr(repo, "record_audit_event"):
            import json

            detail = json.dumps(
                {"cross_owner": True, "target_owner": task_owner, "action": action},
                ensure_ascii=False,
            )
            repo.record_audit_event(
                actor_id=principal_id,
                action="admin_cross_owner_access",
                task_id=task_id,
                request_id=request_id,
                target_type="task",
                target_id=task_id,
                detail_json=detail,
                result="allowed",
            )
        return True

    # 4. 评审员 (reviewer) 跨属主访问 (仅允许只读与验收批准)
    if role == "reviewer" and action in (ACTION_READ, ACTION_APPROVE):
        return True

    # 5. 越权拒绝并记录审计
    if repo and hasattr(repo, "record_audit_event"):
        import json

        detail = json.dumps(
            {"target_owner": task_owner, "attempted_action": action, "role": role},
            ensure_ascii=False,
        )
        repo.record_audit_event(
            actor_id=principal_id,
            action="task_access_denied",
            task_id=task_id,
            request_id=request_id,
            target_type="task",
            target_id=task_id,
            detail_json=detail,
            result="denied",
        )
    return False
