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

EXEMPT_PATHS = frozenset({"/api/health"})


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
