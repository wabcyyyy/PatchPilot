"""命令白名单:Agent 可执行命令的唯一裁决入口。

规则(shell=False 参数列表的前提下):
1. 首个 token 的可执行名必须在白名单内;
2. 任何 token 都不允许出现 shell 元字符——出现即视为注入尝试;
3. 空命令直接拒绝。
"""

from __future__ import annotations

from app.errors import GateError

# 复盘 P1-4:括号不放行会误伤参数化测试 id(如 test_x[(1,2)]——pytest 合法 id,
# 且 nodes 的基线路径直接跑同款命令,形成"基线能跑、Agent 工具不能跑"的口径分裂)。
# shell=False 参数列表下括号无 shell 语义;$ 仍被拒,故 $(cmd) 注入路径依旧拦截
SHELL_METACHARS = (";", "|", "&", "`", ">", "<", "$", "\n", "\r", "\x00")

DEFAULT_WHITELIST: tuple[str, ...] = ("python", "python3", "pytest")


def _exe_name(token: str) -> str:
    """取可执行名:去路径、去 .exe,小写比较。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4].lower() if name.lower().endswith(".exe") else name.lower()


def check_cmd_allowed(
    command: list[str], whitelist: tuple[str, ...] | list[str] = DEFAULT_WHITELIST
) -> None:
    """校验命令;不合法抛 GateError,合法静默返回。"""
    if not command:
        raise GateError("empty command")

    allowed = {w.lower() for w in whitelist}
    exe = _exe_name(command[0])
    if exe not in allowed:
        raise GateError(f"executable {command[0]!r} is not in the allowlist {sorted(allowed)}")

    for token in command:
        for ch in SHELL_METACHARS:
            if ch in token:
                raise GateError(f"shell metachar {ch!r} found in command token {token!r}")
