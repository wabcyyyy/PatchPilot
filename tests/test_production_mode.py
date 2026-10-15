"""P04: 显式生产模式与安全准入测试 (tests/test_production_mode.py)。

钉住以下安全约束:
1. deployment_mode=development/production;
2. Settings.validate_production_readiness 如实校验执行后端、根白名单、可信镜像、有限额度;
3. /api/capabilities 暴露 deployment_mode 与 production_readiness;
4. build_task_spec 生产模式强制要求 docker 隔离后端、非空白名单且路径在白名单内、受信镜像与正数 token 预算;
5. run_preflight 与 create_task 严禁 local 后端、无白名单或越界仓库准入;
6. TaskSpec.effective_policy 忠实冻结 deployment_mode。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.api.capabilities import capabilities
from app.api.preflight import run_preflight
from app.api.service import TaskService, ensure_repo_allowed
from app.config import Settings
from app.errors import InvalidRequestError, TaskError
from app.evals.bugset import load_bug
from app.storage.repository import Repository
from app.task_spec import build_task_spec

BUG_ROOT = Path("bugs")


def test_validate_production_readiness_detects_all_issues() -> None:
    # 默认开发设置在生产模式下应识别出执行后端与白名单等问题
    s_dev = Settings(
        deployment_mode="development",
        execution_backend="local",
        allowed_repo_roots="",
        token_budget=0,
        docker_image="python:3.11-slim",
    )
    issues = s_dev.validate_production_readiness()
    assert any("execution_backend" in i for i in issues)
    assert any("allowed_repo_roots" in i for i in issues)
    assert any("token_budget" in i for i in issues)

    # 镜像不在受信列表
    s_untrusted = Settings(
        execution_backend="docker",
        allowed_repo_roots="D:/repos",
        token_budget=100000,
        docker_image="malicious/image:latest",
        trusted_docker_images="python:3.11-slim,python:3.12-slim",
    )
    issues_untrusted = s_untrusted.validate_production_readiness()
    assert any("not in trusted_docker_images" in i for i in issues_untrusted)

    # 规范的生产配置应全部满足
    s_prod = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots="D:/repos",
        token_budget=100000,
        docker_image="python:3.11-slim",
        trusted_docker_images="python:3.11-slim",
    )
    assert s_prod.validate_production_readiness() == []


def test_capabilities_reports_deployment_mode_and_readiness() -> None:
    s_dev = Settings(deployment_mode="development")
    caps_dev = capabilities(s_dev)
    assert caps_dev["deployment_mode"] == "development"
    assert "production_readiness" in caps_dev
    assert caps_dev["production_readiness"]["ready"] is False
    assert len(caps_dev["production_readiness"]["issues"]) > 0

    s_prod = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots="D:/repos",
        token_budget=100000,
        docker_image="python:3.11-slim",
        trusted_docker_images="python:3.11-slim",
    )
    caps_prod = capabilities(s_prod)
    assert caps_prod["deployment_mode"] == "production"
    assert caps_prod["production_readiness"]["ready"] is True
    assert caps_prod["production_readiness"]["issues"] == []


def test_build_task_spec_freezes_deployment_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    bug = load_bug("BUG-001", BUG_ROOT)
    bug.repo_dir = repo

    s_dev = Settings(deployment_mode="development")
    spec_dev = build_task_spec(
        bug,
        engine="plain",
        arm="agent",
        model_provider="fake-replay",
        max_turns=10,
        settings=s_dev,
    )
    assert spec_dev.effective_policy["deployment_mode"] == "development"


def test_build_task_spec_production_mode_safeguards(tmp_path: Path) -> None:
    repo_allowed = tmp_path / "allowed_repos" / "repo1"
    repo_allowed.mkdir(parents=True)
    repo_outside = tmp_path / "outside_repos" / "repo2"
    repo_outside.mkdir(parents=True)

    bug = load_bug("BUG-001", BUG_ROOT)
    bug.repo_dir = repo_allowed

    # 1. 生产模式拒绝 local 后端
    s_local = Settings(
        deployment_mode="production",
        execution_backend="local",
        allowed_repo_roots=str(tmp_path / "allowed_repos"),
        docker_image="python:3.11-slim",
    )
    with pytest.raises(TaskError, match="local execution is strictly forbidden in production"):
        build_task_spec(
            bug,
            engine="plain",
            arm="agent",
            model_provider="fake-replay",
            max_turns=10,
            settings=s_local,
        )

    # 2. 生产模式拒绝空 allowed_repo_roots
    s_no_roots = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots="",
        docker_image="python:3.11-slim",
    )
    with pytest.raises(TaskError, match="requires non-empty allowed_repo_roots"):
        build_task_spec(
            bug,
            engine="plain",
            arm="agent",
            model_provider="fake-replay",
            max_turns=10,
            settings=s_no_roots,
        )

    # 3. 生产模式拒绝白名单之外的 repo 路径
    bug_outside = load_bug("BUG-001", BUG_ROOT)
    bug_outside.repo_dir = repo_outside
    s_valid = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots=str(tmp_path / "allowed_repos"),
        docker_image="python:3.11-slim",
        trusted_docker_images="python:3.11-slim",
        token_budget=100000,
    )
    with pytest.raises(TaskError, match="outside allowed_repo_roots in production mode"):
        build_task_spec(
            bug_outside,
            engine="plain",
            arm="agent",
            model_provider="fake-replay",
            max_turns=10,
            settings=s_valid,
        )

    # 4. 生产模式拒绝非正数 token 预算
    s_zero_budget = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots=str(tmp_path / "allowed_repos"),
        docker_image="python:3.11-slim",
        trusted_docker_images="python:3.11-slim",
        token_budget=0,
    )
    with pytest.raises(TaskError, match="requires positive token_budget"):
        build_task_spec(
            bug,
            engine="plain",
            arm="agent",
            model_provider="fake-replay",
            max_turns=10,
            settings=s_zero_budget,
        )

    # 5. 生产模式拒绝未受信任的镜像
    s_bad_image = Settings(
        deployment_mode="production",
        execution_backend="docker",
        allowed_repo_roots=str(tmp_path / "allowed_repos"),
        docker_image="untrusted:v1",
        trusted_docker_images="python:3.11-slim",
        token_budget=100000,
    )
    with pytest.raises(TaskError, match="not in trusted_docker_images"):
        build_task_spec(
            bug,
            engine="plain",
            arm="agent",
            model_provider="fake-replay",
            max_turns=10,
            settings=s_bad_image,
        )

    # 6. 全部合规时成功构建
    spec_prod = build_task_spec(
        bug,
        engine="plain",
        arm="agent",
        model_provider="fake-replay",
        max_turns=10,
        settings=s_valid,
    )
    assert spec_prod.effective_policy["deployment_mode"] == "production"
    assert spec_prod.effective_policy["execution_backend"] == "docker"


def test_run_preflight_production_mode_checks(tmp_path: Path) -> None:
    ws_allowed = tmp_path / "allowed" / "ws"
    ws_allowed.mkdir(parents=True)
    ws_outside = tmp_path / "outside" / "ws"
    ws_outside.mkdir(parents=True)

    # 1. 拒绝 local 执行后端
    with pytest.raises(InvalidRequestError, match="local execution is strictly forbidden"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws_allowed,
            failed_tests=["tests/test_x.py::test_ok"],
            regression_tests=["tests/test_x.py::test_ok"],
            deployment_mode="production",
            execution_backend="local",
            allowed_repo_roots=[tmp_path / "allowed"],
            container_image="python:3.11-slim",
            trusted_docker_images={"python:3.11-slim"},
        )

    # 2. 拒绝空白名单
    with pytest.raises(InvalidRequestError, match="requires non-empty allowed_repo_roots"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws_allowed,
            failed_tests=["tests/test_x.py::test_ok"],
            regression_tests=["tests/test_x.py::test_ok"],
            deployment_mode="production",
            execution_backend="docker",
            allowed_repo_roots=[],
            container_image="python:3.11-slim",
            trusted_docker_images={"python:3.11-slim"},
        )

    # 3. 拒绝越界工作区
    with pytest.raises(InvalidRequestError, match="outside allowed_repo_roots"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws_outside,
            failed_tests=["tests/test_x.py::test_ok"],
            regression_tests=["tests/test_x.py::test_ok"],
            deployment_mode="production",
            execution_backend="docker",
            allowed_repo_roots=[tmp_path / "allowed"],
            container_image="python:3.11-slim",
            trusted_docker_images={"python:3.11-slim"},
        )

    # 4. 拒绝未受信镜像
    with pytest.raises(InvalidRequestError, match="not in trusted_docker_images"):
        run_preflight(
            python_exe=sys.executable,
            workspace=ws_allowed,
            failed_tests=["tests/test_x.py::test_ok"],
            regression_tests=["tests/test_x.py::test_ok"],
            deployment_mode="production",
            execution_backend="docker",
            allowed_repo_roots=[tmp_path / "allowed"],
            container_image="evil:latest",
            trusted_docker_images={"python:3.11-slim"},
        )


def test_ensure_repo_allowed_in_production() -> None:
    resolved = Path("D:/secret/repo").resolve()
    # 显式生产模式传空白名单应拒绝
    with pytest.raises(InvalidRequestError, match="production mode requires non-empty"):
        ensure_repo_allowed(resolved, deployment_mode="production")


def test_create_task_production_mode_safeguards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "test.sqlite3")
    service = TaskService(repo=repo, runs_root=tmp_path / "runs")

    # 模拟生产模式但配置了 local 执行后端
    s_bad = Settings(
        deployment_mode="production",
        execution_backend="local",
        allowed_repo_roots=str(tmp_path),
    )
    monkeypatch.setattr("app.api.service.get_settings", lambda: s_bad)
    with pytest.raises(
        InvalidRequestError, match="local execution is strictly forbidden in production"
    ):
        service.create_task(bug_id="BUG-001")
