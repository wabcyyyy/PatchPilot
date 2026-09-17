"""可选 Bearer Token 鉴权:api_token 为空 = 关闭;GET /api/health 恒豁免。"""

from __future__ import annotations

import secrets

from fastapi import Request

from app.config import get_settings

EXEMPT_PATHS = frozenset({"/api/health"})


class UnauthorizedError(Exception):
    """鉴权失败:由 app 层 exception_handler 转统一错误结构(401)。"""


def require_token(request: Request) -> None:
    """Router 级依赖:校验 ``Authorization: Bearer <api_token>``。

    FastAPI 中依赖的返回值不会短路请求,因此失败走 raise,
    统一错误结构 {code, message, task_id} 由 app.py 注册的 exception_handler 输出。
    """
    settings = get_settings()
    if not settings.api_token or request.url.path in EXEMPT_PATHS:
        return
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.api_token}"
    if header and secrets.compare_digest(header.encode(), expected.encode()):
        return
    raise UnauthorizedError("missing or invalid bearer token")
