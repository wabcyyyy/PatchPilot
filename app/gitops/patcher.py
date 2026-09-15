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
    """干跑校验 diff 是否可以干净应用。"""
    _, _, err = run_git(
        Path(workspace),
        "apply",
        "--check",
        "--whitespace=nowarn",
        check=False,
        input_bytes=diff_text.encode("utf-8"),
    )
    ok = not err.strip()
    return ok, err.strip()


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

    run_git(ws, "apply", "--whitespace=nowarn", input_bytes=diff_text.encode("utf-8"))
    log.info("patch applied to %s", ws)
    return PatchApplyResult(True, None, "applied cleanly")
