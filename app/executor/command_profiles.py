"""受控命令 profile(G09):模型可执行命令的唯一装配面。

SPEC §7 G09:`run_command(profile_id, parameters)` 不接受模型提供的 shell 字符串;
profile 由可信的 Settings/运维配置定义并冻结,仓库文件不能自行注册可执行命令。
参数走 argv 构造与语义校验——拒绝越界路径、危险选项、插件/配置注入、可执行名替换;
命令模板不是前缀检查,而是逐参数构造。

安全边界(每条都有对应用例):
- 参数值永不以 ``-`` 开头(选项注入);永不包含 shell 元字符(与 whitelist 同一集合);
- 路径参数必须是工作区内的相对路径(``..`` 与绝对路径当场拒绝);
- pytest 测试 id 只接受 node id 字符集,集合有数量上限;
- ``python_repro`` 只运行经 ``write_repro`` 登记过的脚本(登记名绑定内容 hash),
  任意 ``python -c`` / ``python -m`` 不构成白名单;
- 每个 profile 有自己的 timeout 与输出额度;argv 与 profile hash 一起进回执,可对账。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.errors import GateError
from app.executor.whitelist import SHELL_METACHARS

PROFILES_SCHEMA_VERSION = 1

# 测试 id(node id)字符集:模块路径 + 类/函数 + 参数化括号(参数化 id 允许含空格,
# 复盘 P1-4 的同一条口径:括号与空格在 shell=False 的 argv 里没有 shell 语义);
# 比 whitelist 的元字符拒绝更窄(引号、*、~、; 等一律不进 argv)。
_TEST_ID_RE = re.compile(r"^[A-Za-z0-9_./\[\]():, \-]+$")
_REPRO_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

MAX_TEST_IDS = 64
MAX_PATH_ARGS = 32


class CommandProfileError(GateError):
    """profile 违规:参数注入、越界路径、未登记 repro 等。调用方转结构化失败。"""


@dataclass(frozen=True)
class CommandProfile:
    """一个具名命令模板:可执行、argv 构造器与运行额度全部由平台定义。"""

    profile_id: str
    executable: str
    description: str
    timeout_seconds: int
    docker_only: bool = False  # 通用诊断命令仅允许 Docker;MVP 五个 profile 两种后端皆可

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILES_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "executable": self.executable,
            "description": self.description,
            "timeout_seconds": self.timeout_seconds,
            "docker_only": self.docker_only,
        }


def _reject_meta(value: str) -> str:
    if value.startswith("-"):
        raise CommandProfileError(f"parameter {value!r} must not start with '-' (option injection)")
    for ch in SHELL_METACHARS:
        if ch in value:
            raise CommandProfileError(f"shell metachar {ch!r} in parameter {value!r}")
    return value


def _rel_path(value: str) -> str:
    """工作区内相对路径:绝对路径与 ``..`` 一律拒绝。"""
    _reject_meta(value)
    p = value.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise CommandProfileError(f"absolute path is not allowed: {value!r}")
    parts = p.split("/")
    if any(part == ".." for part in parts):
        raise CommandProfileError(f"path escape is not allowed: {value!r}")
    if not p or p.endswith("/"):
        raise CommandProfileError(f"path must name a file: {value!r}")
    return p


def _test_id(value: str) -> str:
    if not value or not _TEST_ID_RE.match(value):
        raise CommandProfileError(f"invalid test id: {value!r}")
    _reject_meta(value)
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise CommandProfileError(f"path escape in test id: {value!r}")
    return value


def check_test_ids(ids: list[str]) -> list[str]:
    """测试 id 列表的公开校验入口(G10 诊断提议复用同一套语法/注入口径):
    非空、逐个过 node id 字符集、数量上限;返回逐字保留顺序的规范化列表。"""
    if not isinstance(ids, list) or not ids:
        raise CommandProfileError("test id list must be non-empty")
    if len(ids) > MAX_TEST_IDS:
        raise CommandProfileError(f"too many test ids ({len(ids)} > {MAX_TEST_IDS})")
    return [_test_id(str(item)) for item in ids]


def _path_list(parameters: dict[str, Any]) -> list[str]:
    raw = parameters.get("paths")
    if not isinstance(raw, list) or not raw:
        raise CommandProfileError("paths must be a non-empty list")
    if len(raw) > MAX_PATH_ARGS:
        raise CommandProfileError(f"too many paths ({len(raw)} > {MAX_PATH_ARGS})")
    checked = []
    for item in raw:
        path = _rel_path(str(item))
        if not path.endswith(".py"):
            raise CommandProfileError(f"only .py files are accepted: {item!r}")
        checked.append(path)
    return checked


def _test_id_list(parameters: dict[str, Any]) -> list[str]:
    raw = parameters.get("test_ids")
    if not isinstance(raw, list) or not raw:
        raise CommandProfileError("test_ids must be a non-empty list")
    if len(raw) > MAX_TEST_IDS:
        raise CommandProfileError(f"too many test ids ({len(raw)} > {MAX_TEST_IDS})")
    return [_test_id(str(item)) for item in raw]


def _collect_target(parameters: dict[str, Any]) -> list[str]:
    target = parameters.get("target")
    if target in (None, ""):
        return []
    return [_rel_path(str(target))]


def repro_script_name(parameters: dict[str, Any], repro_files: dict[str, str]) -> str:
    """只运行经 write_repro 登记过的脚本:登记名必须命中本任务登记表。"""
    name = parameters.get("repro")
    if not isinstance(name, str) or not name:
        raise CommandProfileError("repro is required (use write_repro first)")
    if not _REPRO_NAME_RE.match(name):
        raise CommandProfileError(f"invalid repro name: {name!r}")
    if name not in repro_files:
        raise CommandProfileError(
            f"repro {name!r} is not registered in this task (write_repro first;"
            " arbitrary python -c/-m is not a whitelisted command)"
        )
    return name


def _argv(executable: str, *parts: str | list[str]) -> list[str]:
    out = [executable]
    for part in parts:
        if isinstance(part, list):
            out.extend(part)
        else:
            out.append(part)
    return out


@dataclass(frozen=True)
class BuiltCommand:
    """校验并构造完成的命令:argv、使用的 profile 与身份 hash。"""

    profile: CommandProfile
    argv: list[str]
    argv_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile.profile_id,
            "argv": self.argv,
            "argv_sha256": self.argv_sha256,
        }


def stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# MVP profile 集(SPEC §7 G09):必要的五个;python_build 仅在镜像已有工具且任务
# 显式启用时另行配置,不在这里默认提供。
PROFILES: dict[str, CommandProfile] = {
    "pytest_collect": CommandProfile(
        profile_id="pytest_collect",
        executable="python",
        description="pytest --collect-only:列出可收集的测试身份",
        timeout_seconds=120,
    ),
    "pytest_selected": CommandProfile(
        profile_id="pytest_selected",
        executable="python",
        description="按显式测试 id 列表运行 pytest(-q)",
        timeout_seconds=120,
    ),
    "python_repro": CommandProfile(
        profile_id="python_repro",
        executable="python",
        description="运行经 write_repro 登记的复现脚本(scratch 内,不进补丁)",
        timeout_seconds=60,
    ),
    "python_compile": CommandProfile(
        profile_id="python_compile",
        executable="python",
        description="py_compile 检查指定 .py 文件可编译",
        timeout_seconds=60,
    ),
    "ruff_check": CommandProfile(
        profile_id="ruff_check",
        executable="ruff",
        description="ruff check 指定 .py 文件(镜像/宿主需预装 ruff)",
        timeout_seconds=60,
    ),
}


def profiles_digest() -> str:
    """全部内置 profile 的身份 hash:进 TaskSpec.effective_policy,profile 集变化可对账。"""
    return stable_hash({pid: p.as_dict() for pid, p in sorted(PROFILES.items())})


def build_command(
    profile_id: str,
    parameters: dict[str, Any] | None,
    *,
    repro_files: dict[str, str] | None = None,
    repro_subdir: str = "",
) -> BuiltCommand:
    """按 profile 校验参数并逐段构造 argv;违规抛 CommandProfileError。

    python_repro 的 argv 是 ``[python, <登记名>]``;调用方把登记脚本拷进执行目录的
    repro_subdir(如 ``.pp-repro``)时传入它,argv 随之指向该相对路径——profile 只做
    校验与构造,不拼接宿主路径,路径拼装只发生在执行层一处。
    """
    profile = PROFILES.get(str(profile_id or ""))
    if profile is None:
        raise CommandProfileError(
            f"unknown command profile {profile_id!r}; available: {sorted(PROFILES)}"
        )
    params = dict(parameters or {})
    exe = profile.executable
    pid = profile.profile_id
    if pid == "pytest_collect":
        argv = _argv(
            exe,
            ["-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            _collect_target(params),
        )
    elif pid == "pytest_selected":
        argv = _argv(
            exe,
            ["-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider"],
            _test_id_list(params),
        )
    elif pid == "python_repro":
        name = repro_script_name(params, repro_files or {})
        prefix = f"{repro_subdir.strip('/')}/" if repro_subdir.strip("/") else ""
        argv = [exe, f"{prefix}{name}"]
    elif pid == "python_compile":
        argv = _argv(exe, ["-m", "py_compile"], _path_list(params))
    elif pid == "ruff_check":
        argv = _argv(exe, ["check"], _path_list(params))
    else:  # pragma: no cover — PROFILES 与这里的分支必须同步
        raise CommandProfileError(f"profile {pid!r} has no argv builder")
    return BuiltCommand(
        profile=profile,
        argv=argv,
        argv_sha256=stable_hash({"profile": pid, "argv": argv}),
    )
