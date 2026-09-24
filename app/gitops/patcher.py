"""补丁应用:`apply_patch` 工具的底层实现。

顺序固定:先 `git apply --check` 干跑校验,再真正应用;
任何格式/上下文/越界问题都在 check 阶段被拒绝,不会留下半套用的补丁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.gitops.cmd import run_git
from app.graph.gates import parse_diff_files, parse_new_files

log = logging.getLogger(__name__)


@dataclass
class PatchApplyResult:
    applied: bool
    rejected_reason: str | None
    detail: str


def _target_violation(workspace: Path, rel: str) -> str | None:
    """对单条 patch 触碰路径做落点校验(ATTACK-009,E1 整改)。

    读侧 `app/tools/paths.py` 已有 resolve+relative_to 防软链,写侧在此补齐:
    ① 落点本身是软链(含悬空软链)→ `symlink_escape`——触碰既有软链即可
       经由它改写工作区外文件,不依赖 git 是否跟随;
    ② 落点 resolve 后越出工作树 → `path_escape`——覆盖 `..` 穿越、绝对路径,
       以及路径中间段是软链时 resolve 跟随解析出的越界落点。
    """
    if not rel:
        return None
    if rel.startswith(("/", "\\")) or rel.startswith("~") or Path(rel).is_absolute():
        return "path_escape"
    target = Path(workspace, rel)
    if target.is_symlink():
        return "symlink_escape"
    try:
        resolved = target.resolve()
        resolved.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return "path_escape"
    return None


def check_patch_targets(workspace: Path | str, diff_text: str) -> PatchApplyResult | None:
    """git apply 前对补丁全部触碰路径做落点校验;通过返回 None。

    触碰路径 = `---`/`+++` 头与 `copy to`/`rename to` 扩展头解析出的相对路径
    (解析与 `app/graph/gates.py` 同源;门禁是静态文本检查,这里是落点对
    真实文件系统的校验,两道独立防线)。返回结构化拒绝结果而非异常,
    与本模块其余拒绝路径同构。
    """
    ws = Path(workspace)
    for rel in [*parse_diff_files(diff_text), *parse_new_files(diff_text)]:
        reason = _target_violation(ws, rel)
        if reason is not None:
            return PatchApplyResult(
                False,
                reason,
                f"[{reason}] patch target rejected before apply: {rel}",
            )
    return None


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

    blocked = check_patch_targets(ws, diff_text)
    if blocked is not None:
        log.info(
            "patch rejected by target check: %s (%s)",
            blocked.rejected_reason,
            blocked.detail,
        )
        return blocked

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
