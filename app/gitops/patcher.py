"""补丁应用:`apply_patch` 工具的底层实现。

顺序固定:先 `git apply --check` 干跑校验,再真正应用;
任何格式/上下文/越界问题都在 check 阶段被拒绝,不会留下半套用的补丁。
应用成功后:① 对全部触碰路径复扫落点校验(TOCTOU 后置收口,见 apply_patch);
② 立即对触及的 .py 文件做语法预检(ast.parse);不通过则把本次
补丁整体还原——语法错误不留在工作区,也不必烧掉一次 pytest 才发现
(rejected_reason = python_syntax_error)。
"""

from __future__ import annotations

import ast
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


def _touched_python_files(diff_text: str) -> list[str]:
    """diff 触及的 .py 相对路径(去重保序);语法预检与还原快照共用。"""
    files: list[str] = []
    for rel in [*parse_diff_files(diff_text), *parse_new_files(diff_text)]:
        if rel.endswith(".py") and rel not in files:
            files.append(rel)
    return files


def _snapshot_files(ws: Path, rels: list[str]) -> dict[str, bytes | None]:
    """应用前快照文件字节内容(不存在记 None),供语法预检失败时精确还原。"""
    snapshot: dict[str, bytes | None] = {}
    for rel in rels:
        target = ws / rel
        snapshot[rel] = target.read_bytes() if target.is_file() else None
    return snapshot


def _restore_files(ws: Path, snapshot: dict[str, bytes | None]) -> list[str]:
    """按快照还原应用前的文件状态;返回还原失败的路径描述(正常应为空)。"""
    failed: list[str] = []
    for rel, content in snapshot.items():
        try:
            if content is None:
                (ws / rel).unlink(missing_ok=True)
            else:
                (ws / rel).write_bytes(content)
        except OSError as exc:
            failed.append(f"{rel} ({exc})")
    return failed


def _syntax_errors(ws: Path, rels: list[str]) -> list[str]:
    """应用后的 Python 语法预检:返回错误描述列表(空 = 通过)。

    触及的 .py 应用后必须通过 `ast.parse`:语法错误要烧掉一次 pytest
    (collection error)才会被模型看到,本预检把这类补丁在落盘前拦下。
    被删除的文件跳过;错误里带新文件内的行列号,模型可直接据此修正。
    """
    errors: list[str] = []
    for rel in rels:
        target = ws / rel
        if not target.is_file():
            continue
        try:
            ast.parse(target.read_bytes(), filename=rel)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else "?"
            col = exc.offset if exc.offset is not None else "?"
            errors.append(f"{rel}:{line}:{col}: {exc.msg}")
        except OSError as exc:
            errors.append(f"{rel}: unreadable for syntax check: {exc}")
        except ValueError as exc:
            # compile 级错误(如源码含空字节)
            errors.append(f"{rel}: {exc}")
    return errors


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

    py_files = _touched_python_files(diff_text)
    snapshot = _snapshot_files(ws, py_files)

    rc, _, err = run_git(
        ws, "apply", "--whitespace=nowarn", check=False, input_bytes=diff_text.encode("utf-8")
    )
    if rc != 0:
        # P2-8 整改:--check 与真 apply 之间工作区不会变,但防御性收敛为
        # 结构化结果,与 docstring 一致,不再抛异常层的 GitCmdError(RuntimeError)
        log.warning("patch apply failed rc=%s: %s", rc, err.splitlines()[0] if err else "unknown")
        return PatchApplyResult(False, "git apply failed", err)

    # 复盘 R-3(TOCTOU 后置复扫):check_patch_targets 与真 apply 之间是时间窗,
    # 窗口内落点被换成软链/越界时不再依赖"工具串行所以没人能换"的时序假设——
    # apply 后对全部触碰路径重跑落点校验,违规即 git apply -R 反向还原,
    # 按结构化拒绝收口(同一防线也拦住绕过上层静态门禁直灌 patcher 的调用方)
    touched = [*parse_diff_files(diff_text), *parse_new_files(diff_text)]
    post_violation = next(
        ((rel, reason) for rel in touched if (reason := _target_violation(ws, rel)) is not None),
        None,
    )
    if post_violation is not None:
        rel, reason = post_violation
        rc_r, _, err_r = run_git(
            ws,
            "apply",
            "-R",
            "--whitespace=nowarn",
            check=False,
            input_bytes=diff_text.encode("utf-8"),
        )
        detail = (
            f"[{reason}] target violated after apply (TOCTOU recheck): {rel};"
            f" patch reverted (reverse apply rc={rc_r}"
            + (f", err={err_r.strip().splitlines()[0]}" if rc_r != 0 and err_r.strip() else "")
            + ")"
        )
        log.warning("patch rejected by post-apply recheck: %s", detail)
        return PatchApplyResult(False, reason, detail)

    syntax_errors = _syntax_errors(ws, py_files)
    if syntax_errors:
        restore_failed = _restore_files(ws, snapshot)
        detail_text = (
            "patch leaves Python syntax error(s) in touched file(s);"
            " all changes from this patch were reverted:\n"
            + "\n".join(f"  - {e}" for e in syntax_errors)
        )
        if restore_failed:
            detail_text += "\nrestore failed for: " + "; ".join(restore_failed)
        log.warning("patch rejected by syntax precheck: %s", syntax_errors[0])
        return PatchApplyResult(False, "python_syntax_error", detail_text)

    log.info("patch applied to %s", ws)
    return PatchApplyResult(True, None, "applied cleanly")
