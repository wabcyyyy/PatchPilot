"""统一数据策略、秘密过滤与模型出口控制 (P06-1)。

规范要求:
1. 统一秘密路径判定: 拒读与禁止遍历敏感凭据 (*.key, *.pem, id_rsa*, .env* 等);
2. 文本秘密过滤: 覆盖模型提示、工具输出、日志、事件与报告 (私钥块、API Key、Bearer Token、密码);
3. 模型出口限制: 生产模式下严禁任意更改模型端点, 必须命中白名单 (PATCHPILOT_ALLOWED_MODEL_ENDPOINTS);
4. 原始事实与公开视图隔离: 不改变原始工件与判定事实, 只在公开/外发视图做脱敏。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.errors import TaskError

DATA_POLICY_VERSION = "1"

SECRET_EXTENSIONS = frozenset({".pem", ".key", ".pfx", ".p12", ".crt", ".der", ".kdbx"})
SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")

# 正则模式
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]+PRIVATE KEY-----",
    re.MULTILINE,
)
_TOKEN_PREFIX_RE = re.compile(r"\b(?:sk|pk|ghp|gho|ghu|ghs|ghr)[-_][a-zA-Z0-9_\-]{16,}\b")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([a-zA-Z0-9_\-\.]{16,})")
_KEY_VAL_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\"']?)([a-zA-Z0-9_\-\.]{12,})([\"']?)"
)
_PASSWORD_RE = re.compile(
    r"(?i)\b((?:password|passwd|pwd)\s*[:=]\s*[\"']?)([^\"'\s\r\n]{6,})([\"']?)"
)
_CONN_STRING_RE = re.compile(
    r"(?i)\b((?:postgres|postgresql|mysql|redis|mongodb)://[^:]+:)([^@]+)(@)"
)


def is_secret_path(path: str | Path) -> bool:
    """判定路径是否属于应拦截的秘密或敏感凭据文件。"""
    p = Path(path)
    parts = p.parts
    # 任何层级包含 .env (排除 .env.example)
    for part in parts:
        if part.startswith(".env") and part != ".env.example":
            return True

    name = p.name
    if not name:
        return False

    suffix = p.suffix.lower()
    if suffix in SECRET_EXTENSIONS:
        return True

    if any(name.startswith(prefix) for prefix in SECRET_PREFIXES):
        return True

    lower_name = name.lower()
    return bool(
        "credential" in lower_name
        and not (
            lower_name.endswith(".lock")
            or lower_name.endswith(".json")
            or lower_name.endswith(".py")
            or lower_name.endswith(".example")
            or lower_name.endswith(".md")
        )
    )


def redact_secrets(text: str) -> str:
    """对文本中的敏感信息进行脱敏过滤，替换为结构化占位符。"""
    if not text:
        return text

    # 1. 替换私钥块
    text = _PRIVATE_KEY_RE.sub("[REDACTED_SECRET:PRIVATE_KEY]", text)
    # 2. 替换带前缀的标准 API Key (sk-, ghp-, etc.)
    text = _TOKEN_PREFIX_RE.sub("[REDACTED_SECRET:TOKEN]", text)
    # 3. 替换 Bearer Token
    text = _BEARER_RE.sub(r"\1[REDACTED_SECRET:BEARER_TOKEN]", text)
    # 4. 替换键值对中的 Token 与 API Key
    text = _KEY_VAL_RE.sub(r"\1[REDACTED_SECRET:TOKEN]\3", text)
    # 5. 替换明文密码
    text = _PASSWORD_RE.sub(r"\1[REDACTED_SECRET:PASSWORD]\3", text)
    # 6. 替换数据库/Redis 连接串中的密码
    return _CONN_STRING_RE.sub(r"\1[REDACTED_SECRET:PASSWORD]\3", text)


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "x-api-key",
    }
)


def redact_data(data: Any) -> Any:
    """递归对结构化数据（dict, list, tuple, str）进行脱敏。"""
    if isinstance(data, str):
        return redact_secrets(data)
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEY_NAMES:
                out[k] = (
                    "[REDACTED_SECRET:PASSWORD]"
                    if ("pass" in k.lower() or "pwd" in k.lower())
                    else "[REDACTED_SECRET:TOKEN]"
                )
            else:
                out[k] = redact_data(v)
        return out
    if isinstance(data, list):
        return [redact_data(v) for v in data]
    if isinstance(data, tuple):
        return tuple(redact_data(v) for v in data)
    return data


def validate_model_endpoint(
    endpoint: str,
    allowed_endpoints: str,
    *,
    deployment_mode: str = "development",
) -> None:
    """校验模型调用目的地是否属于批准的出口白名单。"""
    if deployment_mode != "production":
        return

    raw = allowed_endpoints.strip()
    if not raw:
        raise TaskError("production mode requires non-empty PATCHPILOT_ALLOWED_MODEL_ENDPOINTS")

    norm_target = endpoint.strip().rstrip("/").lower()
    target_parsed = urlparse(norm_target)
    target_host = target_parsed.netloc or target_parsed.path

    allowed_list = [p.strip().rstrip("/").lower() for p in raw.split(",") if p.strip()]
    for candidate in allowed_list:
        cand_parsed = urlparse(candidate)
        cand_host = cand_parsed.netloc or cand_parsed.path
        if norm_target == candidate or norm_target.startswith(candidate + "/"):
            return
        if target_host and cand_host and target_host == cand_host:
            return

    raise TaskError(
        f"model endpoint '{endpoint}' is not in allowed_model_endpoints in production mode"
    )
