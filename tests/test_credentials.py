"""P02 凭据生命周期与主体认证测试。"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agent.cli import main as cli_main
from app.storage.repository import Repository


def test_principal_and_credential_lifecycle(tmp_path: Path) -> None:
    """主体创建、凭据签发、单向散列存储、认证与吊销闭环。"""
    repo = Repository(tmp_path / "creds.sqlite3")

    # 1. 创建主体
    principal = repo.create_principal(
        principal_id="pr_analyst_01",
        name="Security Analyst",
        role="reviewer",
    )
    assert principal["id"] == "pr_analyst_01"
    assert principal["role"] == "reviewer"
    assert principal["is_active"] == 1

    # 2. 签发凭据
    cred, raw_key = repo.issue_credential(principal_id="pr_analyst_01")
    assert raw_key.startswith("pp_live_")
    assert cred["principal_id"] == "pr_analyst_01"
    assert cred["prefix"] == raw_key[:12]
    assert cred["revoked_at"] is None

    # 验证数据库中未明文保存 raw_key
    raw_conn = sqlite3.connect(str(tmp_path / "creds.sqlite3"))
    raw_row = raw_conn.execute("SELECT * FROM credentials WHERE id = ?", (cred["id"],)).fetchone()
    raw_conn.close()
    assert raw_key not in str(raw_row)

    # 3. 凭据认证成功
    auth_res = repo.authenticate_credential(raw_key)
    assert auth_res is not None
    auth_p, auth_c = auth_res
    assert auth_p["id"] == "pr_analyst_01"
    assert auth_c["id"] == cred["id"]

    # 4. 吊销凭据后立即失效
    assert repo.revoke_credential(cred["id"]) is True
    # 重复吊销返回 False
    assert repo.revoke_credential(cred["id"]) is False

    # 吊销后认证失败
    assert repo.authenticate_credential(raw_key) is None


def test_credential_expiration(tmp_path: Path) -> None:
    """过期凭据不可认证。"""
    repo = Repository(tmp_path / "exp.sqlite3")
    repo.create_principal(principal_id="pr_user", name="Alice", role="member")

    past_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
    _, expired_key = repo.issue_credential(principal_id="pr_user", expires_at=past_time)
    assert repo.authenticate_credential(expired_key) is None

    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="milliseconds")
    _, valid_key = repo.issue_credential(principal_id="pr_user", expires_at=future_time)
    assert repo.authenticate_credential(valid_key) is not None


def test_credential_rotation(tmp_path: Path) -> None:
    """凭据轮换：支持无宽限期即时吊销与带宽限期平滑过渡。"""
    repo = Repository(tmp_path / "rot.sqlite3")
    repo.create_principal(principal_id="pr_dev", name="Bob", role="admin")

    # 1. 签发初代凭据
    old_cred, old_key = repo.issue_credential(principal_id="pr_dev")

    # 2. 带宽限期轮换 (例如 3600 秒)
    new_cred, new_key = repo.rotate_credential(old_cred["id"], grace_seconds=3600)
    assert new_key != old_key
    assert new_cred["principal_id"] == "pr_dev"

    # 新旧两把 Key 在宽限期内均有效
    assert repo.authenticate_credential(old_key) is not None
    assert repo.authenticate_credential(new_key) is not None

    # 3. 无宽限期轮换 (即时吊销)
    final_cred, final_key = repo.rotate_credential(new_cred["id"], grace_seconds=0)
    assert final_cred["principal_id"] == "pr_dev"
    assert repo.authenticate_credential(new_key) is None
    assert repo.authenticate_credential(final_key) is not None

    # 对已吊销的 Key 再次轮换抛 ValueError
    with pytest.raises(ValueError, match="cannot rotate revoked credential"):
        repo.rotate_credential(new_cred["id"])


def test_admin_cli_key_management(tmp_path: Path) -> None:
    """通过管理 CLI 执行主体创建与 Key 签发、吊销、轮换。"""
    db_path = str(tmp_path / "admin_cli.sqlite3")

    # 1. 创建主体
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "admin",
                "--db",
                db_path,
                "create-principal",
                "--id",
                "pr_ops",
                "--name",
                "Ops Agent",
                "--role",
                "service_account",
            ]
        )
    assert rc == 0
    p_data = json.loads(buf.getvalue())
    assert p_data["id"] == "pr_ops"

    # 2. 签发 Key
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["admin", "--db", db_path, "issue-key", "--principal-id", "pr_ops"])
    assert rc == 0
    issue_data = json.loads(buf.getvalue())
    raw_key = issue_data["api_key"]
    cred_id = issue_data["credential"]["id"]
    assert raw_key.startswith("pp_live_")

    # 3. 列举 Key
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["admin", "--db", db_path, "list-keys"])
    assert rc == 0
    keys = json.loads(buf.getvalue())
    assert len(keys) == 1
    assert keys[0]["id"] == cred_id

    # 4. 吊销 Key
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["admin", "--db", db_path, "revoke-key", "--key-id", cred_id])
    assert rc == 0
    revoke_data = json.loads(buf.getvalue())
    assert revoke_data["revoked"] is True
