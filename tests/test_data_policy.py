"""P06-1: 数据策略、秘密过滤与模型出口控制测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import TaskError
from app.security.data_policy import (
    is_secret_path,
    redact_data,
    redact_secrets,
    validate_model_endpoint,
)
from app.tools.base import ToolContext
from app.tools.files import collect_repo_files, read_file


def test_is_secret_path_detection() -> None:
    # 敏感扩展与私钥
    assert is_secret_path("certs/server.pem") is True
    assert is_secret_path("keys/priv.key") is True
    assert is_secret_path("keys/server.crt") is True
    assert is_secret_path("keys/id_rsa") is True
    assert is_secret_path(".ssh/id_ed25519") is True

    # 环境与凭据文件
    assert is_secret_path(".env") is True
    assert is_secret_path(".env.local") is True
    assert is_secret_path("config/.env.prod") is True
    assert is_secret_path(".env.example") is False  # 明确放行模板

    # 普通源码与测试
    assert is_secret_path("app/main.py") is False
    assert is_secret_path("tests/test_api.py") is False
    assert is_secret_path("poetry.lock") is False
    assert is_secret_path("credentials.py") is False


def test_redact_secrets_text() -> None:
    # 1. 私钥块
    pk = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0\n-----END RSA PRIVATE KEY-----"
    assert redact_secrets(pk) == "[REDACTED_SECRET:PRIVATE_KEY]"

    # 2. 标准前缀 Token
    assert (
        redact_secrets("Token is sk-1234567890abcdef12345678") == "Token is [REDACTED_SECRET:TOKEN]"
    )
    assert (
        redact_secrets("GitHub: ghp_1234567890abcdef12345678") == "GitHub: [REDACTED_SECRET:TOKEN]"
    )

    # 3. Bearer Token
    assert (
        redact_secrets("Authorization: Bearer mysecrettoken12345678")
        == "Authorization: Bearer [REDACTED_SECRET:BEARER_TOKEN]"
    )

    # 4. 键值对中的 API Key 与 密码
    kv = "api_key='secret_key_1234567890' password='super_secret_pw'"
    redacted = redact_secrets(kv)
    assert "secret_key_1234567890" not in redacted
    assert "super_secret_pw" not in redacted
    assert "[REDACTED_SECRET:TOKEN]" in redacted
    assert "[REDACTED_SECRET:PASSWORD]" in redacted

    # 5. 连接串
    conn = "postgres://admin:topsecret123@localhost:5432/mydb"
    assert redact_secrets(conn) == "postgres://admin:[REDACTED_SECRET:PASSWORD]@localhost:5432/mydb"


def test_redact_data_nested_structures() -> None:
    data = {
        "text": "Bearer mysecrettoken12345678",
        "nested": [
            {"password": "secretpassword123"},
            "plain string",
        ],
        "number": 42,
    }
    cleaned = redact_data(data)
    assert cleaned["number"] == 42
    assert "mysecrettoken12345678" not in cleaned["text"]
    assert "[REDACTED_SECRET:BEARER_TOKEN]" in cleaned["text"]
    assert "secretpassword123" not in str(cleaned["nested"])


def test_validate_model_endpoint_dev_vs_prod() -> None:
    # 开发模式不做限制
    validate_model_endpoint(
        "http://untrusted.com/v1",
        "",
        deployment_mode="development",
    )

    # 生产模式缺少白名单时拒绝
    with pytest.raises(TaskError, match="non-empty PATCHPILOT_ALLOWED_MODEL_ENDPOINTS"):
        validate_model_endpoint(
            "https://api.openai.com/v1",
            "",
            deployment_mode="production",
        )

    # 生产模式命中白名单
    allowed = "https://api.openai.com/v1, https://internal-llm.corp.net"
    validate_model_endpoint(
        "https://api.openai.com/v1",
        allowed,
        deployment_mode="production",
    )
    validate_model_endpoint(
        "https://internal-llm.corp.net/v1/chat/completions",
        allowed,
        deployment_mode="production",
    )

    # 生产模式未命中白名单拒绝
    with pytest.raises(TaskError, match="not in allowed_model_endpoints"):
        validate_model_endpoint(
            "https://evil.external-service.org/v1",
            allowed,
            deployment_mode="production",
        )


def test_read_file_and_collect_files_security(tmp_path: Path) -> None:
    # 构造测试工作区
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_KEY=sk-1234567890abcdef12345", encoding="utf-8")
    (tmp_path / "cert.pem").write_text("PRIVATE_KEY", encoding="utf-8")
    (tmp_path / "app" / "config.py").write_text(
        "api_key = 'sk-1234567890abcdef12345678'\nnormal = 1",
        encoding="utf-8",
    )

    # 1. collect_repo_files 过滤敏感文件
    files, _ = collect_repo_files(tmp_path, None, 100)
    assert "app/main.py" in files
    assert "app/config.py" in files
    assert ".env" not in files
    assert "cert.pem" not in files

    from app.tools.tracker import Tracker

    ctx = ToolContext(
        task_id="T-SEC",
        workspace=tmp_path,
        baseline_commit="HEAD",
        tracker=Tracker(tmp_path / "traj.jsonl", task_id="T-SEC"),
        max_read_lines=100,
        max_search_results=100,
    )

    # 2. read_file 拒读敏感路径
    res_env = read_file(ctx, ".env")
    assert not res_env.ok
    assert "refusing to read secret or sensitive path" in (res_env.error or "")

    res_pem = read_file(ctx, "cert.pem")
    assert not res_pem.ok
    assert "refusing to read secret or sensitive path" in (res_pem.error or "")

    # 3. read_file 普通代码中敏感字符串被脱敏
    res_code = read_file(ctx, "app/config.py")
    assert res_code.ok
    assert "sk-1234567890abcdef12345678" not in res_code.output["content"]
    assert "[REDACTED_SECRET:TOKEN]" in res_code.output["content"]
    assert "normal = 1" in res_code.output["content"]
