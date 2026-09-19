"""补丁应用:`apply_patch` 工具的底层实现。

顺序固定:先 `git apply --check` 干跑校验,再真正应用;
任何格式/上下文/越界问题都在 check 阶段被拒绝,不会留下半套用的补丁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.gitops.cmd import run_git

log = logging.getLogger(__name__)


@dataclass
class PatchApplyResult:
    applied: bool
    rejected_reason: str | None
    detail: str


def check_patch(workspace: Path | str, diff_text: str) -> tuple[bool, str]:
    """干跑校验 diff 是否可以干净应用。

    P2-8 整改:以返回码为准——此前以"stderr 为空"判成败,会忽略 rc,
    而 git 的警告性输出(如行尾归一提示)伴随 rc=0 出现时会被误判为失败。
    """
    rc, _, err = run_git(
        Path(workspace),
        "apply",
        "--check",
        "--whitespace=nowarn",
        check=False,
        input_bytes=diff_text.encode("utf-8"),
    )
    return rc == 0, err.strip()


def apply_patch(workspace: Path | str, diff_text: str) -> PatchApplyResult:
    """把 unified diff 应用到工作区;失败时返回结构化结果而不是抛异常。

    供 Agent 工具层与门禁使用:调用方根据 applied/rejected_reason 决定状态转移。
    """
    ws = Path(workspace)
    if not diff_text.strip():
        return PatchApplyResult(False, "empty patch", "diff text is empty")

    ok, detail = check_patch(ws, diff_text)
    if not ok:
        log.info("patch rejected by --check: %s", detail.splitlines()[0] if detail else "unknown")
        return PatchApplyResult(False, "git apply --check failed", detail)

    rc, _, err = run_git(
        ws, "apply", "--whitespace=nowarn", check=False, input_bytes=diff_text.encode("utf-8")
    )
    if rc != 0:
        # P2-8 整改:--check 与真 apply 之间工作区不会变,但防御性收敛为
        # 结构化结果,与 docstring 一致,不再抛异常层的 GitCmdError(RuntimeError)
        log.warning("patch apply failed rc=%s: %s", rc, err.splitlines()[0] if err else "unknown")
        return PatchApplyResult(False, "git apply failed", err)
    log.info("patch applied to %s", ws)
    return PatchApplyResult(True, None, "applied cleanly")
