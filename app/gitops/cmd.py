"""git 命令执行的薄封装:统一解码、超时与错误信息。"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class GitCmdError(RuntimeError):
    """git 命令非零退出。"""


def run_git(
    cwd: Path | str,
    *args: str,
    check: bool = True,
    timeout: float = 60.0,
    input_bytes: bytes | None = None,
) -> tuple[int, str, str]:
    """在 cwd 下执行 `git -C cwd <args>`,返回 (returncode, stdout, stderr)。

    text 模式由本函数统一控制:字节解码为 UTF-8(替换错误字符),
    避免 Windows 控制台 GBK 编码问题。
    """
    cmd = ["git", "-C", str(cwd), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            input=input_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCmdError(f"git {' '.join(args)} timed out after {timeout}s") from exc

    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GitCmdError(f"git {' '.join(args)} failed rc={proc.returncode}: {err.strip()}")
    log.debug("git %s rc=%s", " ".join(args), proc.returncode)
    return proc.returncode, out, err
