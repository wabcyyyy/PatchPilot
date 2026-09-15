"""质量门禁引擎(企划书第 9 节六项门禁)。

判定规则属于"必须掌握"区:任何一项不满足,补丁不得进入 VERIFY。
门禁是纯文本/纯参数检查,不碰工作区;真正的 `git apply --check` 在 patcher 层。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch

from app.errors import BudgetError, GateError
from app.tools.paths import is_test_file, normalize_rel, path_allowed

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)
_PLUSPLUS_RE = re.compile(r"^\+\+\+ (.+)$", re.MULTILINE)


@dataclass
class GateViolation:
    gate: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.gate}] {self.detail}"


@dataclass
class GateReport:
    ok: bool
    violations: list[GateViolation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def parse_diff_files(diff_text: str) -> list[str]:
    """从 unified diff 提取(去重、保序的)变更文件相对路径列表。"""
    files: list[str] = []

    def _add(candidate: str) -> None:
        cand = candidate.strip()
        if cand in ("/dev/null", ""):
            return
        norm = normalize_rel(cand[2:] if cand.startswith(("b/", "a/")) else cand)
        if norm and norm not in files:
            files.append(norm)

    for match in _DIFF_GIT_RE.finditer(diff_text):
        _add(match.group(2))
    for match in _PLUSPLUS_RE.finditer(diff_text):
        _add(match.group(1))
    return files


def run_gates(
    diff_text: str,
    *,
    allowed_paths: list[str] | None = None,
    max_files: int = 5,
    forbid_test_files: bool = True,
) -> GateReport:
    """对补丁执行格式/文件/路径/范围四项静态门禁。"""
    violations: list[GateViolation] = []

    # 1. 格式门禁:必须像 unified diff(完整校验由 git apply --check 兜底)
    if not diff_text.strip():
        violations.append(GateViolation("format", "diff is empty"))
    elif "diff --git" not in diff_text and not diff_text.startswith(("--- ", "+++ ")):
        violations.append(GateViolation("format", "not a unified diff"))

    files = parse_diff_files(diff_text)
    if not files and not any(v.gate == "format" for v in violations):
        violations.append(GateViolation("format", "no changed files parsed from diff"))

    for rel in files:
        # 2. 文件门禁:禁止修改测试文件(防止 Agent 改测试伪造通过)
        if forbid_test_files and is_test_file(rel):
            violations.append(GateViolation("files", f"modifying test file is forbidden: {rel}"))
        # 3. 路径门禁:穿越与绝对路径;以及 allowed_paths 白名单
        parts = normalize_rel(rel).split("/")
        if ".." in parts or rel.startswith(("/", "\\")) or ":" in rel.split("/")[0]:
            violations.append(GateViolation("paths", f"path escapes workspace: {rel}"))
            continue
        if not path_allowed(rel, allowed_paths):
            violations.append(GateViolation("paths", f"path outside allowed scope: {rel}"))

    # 4. 范围门禁:修改文件数上限
    if len(files) > max_files:
        violations.append(GateViolation("scope", f"{len(files)} files changed, max is {max_files}"))

    return GateReport(ok=not violations, violations=violations)


def ensure_command_allowed(command: list[str], whitelist: tuple[str, ...]) -> None:
    """第 5 项(命令门禁):执行前白名单校验,由工具层调用。"""
    from app.executor.whitelist import check_cmd_allowed

    check_cmd_allowed(command, whitelist)


def ensure_budget(
    *,
    round_no: int,
    max_rounds: int,
    tokens_used: int,
    token_budget: int,
    started_monotonic: float,
    time_budget_seconds: int,
) -> None:
    """第 6 项(资源门禁):轮数/token/时间任一超限即 BudgetError。"""
    if round_no > max_rounds:
        raise BudgetError(f"round {round_no} exceeds max_rounds={max_rounds}")
    if token_budget > 0 and tokens_used > token_budget:
        raise BudgetError(f"tokens {tokens_used} exceed budget {token_budget}")
    if time_budget_seconds > 0 and time.monotonic() - started_monotonic > time_budget_seconds:
        raise BudgetError(f"task exceeded time budget {time_budget_seconds}s")
