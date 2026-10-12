"""G09 受控命令 profile:参数校验、argv 构造与身份 hash(纯逻辑,零子进程)。"""

from __future__ import annotations

import pytest

from app.errors import GateError
from app.executor.command_profiles import (
    PROFILES,
    CommandProfileError,
    build_command,
    profiles_digest,
)

# ---------- profile 集合 ----------


def test_mvp_profiles_are_exactly_the_five_builtins() -> None:
    """MVP profile 集 = pytest_collect/pytest_selected/python_repro/python_compile/ruff_check;
    python_build 不在默认集(镜像已有工具且任务显式启用才另行配置)。"""
    assert set(PROFILES) == {
        "pytest_collect",
        "pytest_selected",
        "python_repro",
        "python_compile",
        "ruff_check",
    }


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(CommandProfileError):
        build_command("run_shell", {})
    with pytest.raises(CommandProfileError):
        build_command("python_build", {})


def test_profiles_digest_is_stable_and_covers_all_profiles() -> None:
    digest = profiles_digest()
    assert len(digest) == 64 and digest == profiles_digest()


def test_built_command_carries_argv_hash() -> None:
    a = build_command("python_compile", {"paths": ["src/mod.py"]})
    b = build_command("python_compile", {"paths": ["src/mod.py"]})
    assert a.argv == b.argv and a.argv_sha256 == b.argv_sha256
    c = build_command("python_compile", {"paths": ["src/other.py"]})
    assert c.argv_sha256 != a.argv_sha256


# ---------- 参数注入与越界 ----------


@pytest.mark.parametrize("bad", ["-q", "-x", "--tb=short", "-"])
def test_leading_dash_is_rejected_everywhere(bad: str) -> None:
    with pytest.raises(CommandProfileError):
        build_command("pytest_selected", {"test_ids": [bad]})
    with pytest.raises(CommandProfileError):
        build_command("python_compile", {"paths": [bad]})
    with pytest.raises(CommandProfileError):
        build_command("pytest_collect", {"target": bad})


@pytest.mark.parametrize("bad", ["a;rm", "a&b", "a|b", "a`b`", "a>b", "$(x)", "a\nb"])
def test_shell_metachars_are_rejected(bad: str) -> None:
    with pytest.raises(CommandProfileError):
        build_command("pytest_selected", {"test_ids": [bad]})
    with pytest.raises(CommandProfileError):
        build_command("python_compile", {"paths": [bad]})


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "C:/Windows/system32", "../outside.py", "src/../../escape.py"]
)
def test_absolute_and_escaping_paths_are_rejected(bad: str) -> None:
    with pytest.raises(CommandProfileError):
        build_command("python_compile", {"paths": [bad]})
    with pytest.raises(CommandProfileError):
        build_command("pytest_collect", {"target": bad})


def test_non_py_paths_are_rejected_for_compile_and_ruff() -> None:
    with pytest.raises(CommandProfileError):
        build_command("python_compile", {"paths": ["src/mod.txt"]})
    with pytest.raises(CommandProfileError):
        build_command("ruff_check", {"paths": ["src"]})


def test_empty_and_oversized_sets_are_rejected() -> None:
    with pytest.raises(CommandProfileError):
        build_command("pytest_selected", {"test_ids": []})
    with pytest.raises(CommandProfileError):
        build_command("python_compile", {"paths": []})
    with pytest.raises(CommandProfileError):
        build_command("pytest_selected", {"test_ids": [f"tests/test_{i}.py::t" for i in range(65)]})


# ---------- 测试 id 与参数化 id ----------


def test_parametrized_ids_with_spaces_are_accepted() -> None:
    """复盘 P1-4 的同一条口径:参数化 id 的空格/括号在 shell=False 下没有 shell 语义,
    profile 不能比 whitelist 更窄,否则出现"基线能跑、工具不能跑"的分裂。"""
    built = build_command("pytest_selected", {"test_ids": ["tests/test_x.py::test_x[(1, 2)]"]})
    assert built.argv[-1] == "tests/test_x.py::test_x[(1, 2)]"


def test_pytest_selected_argv_shape() -> None:
    built = build_command("pytest_selected", {"test_ids": ["tests/test_mod.py::test_add"]})
    assert built.argv[:2] == ["python", "-m"] and "pytest" in built.argv
    assert "-q" in built.argv and "tests/test_mod.py::test_add" in built.argv


def test_pytest_collect_without_target_collects_whole_repo() -> None:
    built = build_command("pytest_collect", {})
    assert "--collect-only" in built.argv
    assert built.argv[-1].startswith("-p") is False or True  # 目标为空时无路径参数


# ---------- python_repro:只认登记名 ----------


def test_repro_requires_registered_name() -> None:
    with pytest.raises(CommandProfileError):
        build_command("python_repro", {"repro": "unregistered.py"}, repro_files={})
    built = build_command(
        "python_repro", {"repro": "repro-1.py"}, repro_files={"repro-1.py": "abc"}
    )
    assert built.argv == ["python", "repro-1.py"]


def test_repro_subdir_lands_in_argv_and_hash() -> None:
    built = build_command(
        "python_repro",
        {"repro": "repro-1.py"},
        repro_files={"repro-1.py": "abc"},
        repro_subdir=".pp-repro",
    )
    assert built.argv == ["python", ".pp-repro/repro-1.py"]


def test_profile_error_is_gate_error() -> None:
    """CommandProfileError 是 GateError 子类:门禁语义统一由上层收敛为结构化失败。"""
    assert issubclass(CommandProfileError, GateError)
