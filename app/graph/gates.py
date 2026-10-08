"""质量门禁引擎(企划书第 9 节七项门禁)。

七项的落点:静态五项(格式/文件/路径/影子/范围)在 run_gates,
命令门禁在 ensure_command_allowed,资源门禁在 ensure_budget;
格式门禁的 `git apply --check` 兜底在 patcher 层。

判定规则属于"必须掌握"区:任何一项不满足,补丁不得进入 VERIFY。
门禁是纯文本/纯参数检查,不碰工作区。
"""

from __future__ import annotations

import importlib.machinery
import re
import sys
import time
from dataclasses import dataclass, field

from app.errors import BudgetError
from app.tools.paths import is_test_file, normalize_rel, path_allowed

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)
_PLUSPLUS_RE = re.compile(r"^\+\+\+ (.+)$", re.MULTILINE)
_NEW_FILE_RE = re.compile(r"^new file mode ", re.MULTILINE)
_RENAME_TO_RE = re.compile(r"^rename to (.+)$", re.MULTILINE)
_COPY_TO_RE = re.compile(r"^copy to (.+)$", re.MULTILINE)
_NEW_FILE_MODE_RE = re.compile(r"^new file mode (\d+)", re.MULTILINE)
# 120000 = git 软链模式;合法落点新建软链可让 verify 阶段 import 工作区外模块
# (审计 R3-Q4 两端实证),一律拒绝——门禁判定是纯文本的,跨平台一致。
_SYMLINK_MODE = "120000"

# N-1 整改:`python -m pytest` 把 cwd(=工作区)置于 sys.path[0],工作区根下与
# 解释器/工具链同名的顶层模块会遮蔽真身——影子 pytest 能读 argv 里的 --junitxml,
# 伪造"期望 id 全部通过"的报告并 exit 0,判定层整体失守。故禁止新增此类顶层模块
# (修改既有文件不在此列;基线仓库本身可信由部署方保证)。
_RESERVED_TOP_LEVEL = frozenset(sys.stdlib_module_names) | {
    "pytest",
    "_pytest",
    "py",
    "pluggy",
    "iniconfig",
    "execnet",
    # site 初始化阶段即被解释器导入的钩子名(不在 stdlib_module_names)
    "sitecustomize",
    "usercustomize",
}
# R2 整改:可导入后缀不止 .py——.pyd(windows)/.so(posix)/.pyc 同样能成为
# 影子模块,顶层段比对前先剥掉全部可导入后缀取模块名。
# 必须用静态全集而不是 importlib.machinery:门禁结论要跨平台可复现(P2-6),
# Windows 解释器的 EXTENSION_SUFFIXES 不含 .so,反之亦然。
_IMPORTABLE_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    *tuple(
        s
        for s in importlib.machinery.all_suffixes()
        if s not in (".py", ".pyc", ".pyo", ".pyd", ".so")
    ),
)


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


def parse_new_files(diff_text: str) -> list[str]:
    """从 unified diff 提取"新增文件"(含 new file mode 段)的相对路径,保序去重。"""
    new_files: list[str] = []
    matches = list(_DIFF_GIT_RE.finditer(diff_text))
    for i, match in enumerate(matches):
        section_start = match.end()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        section = diff_text[section_start:section_end]
        if _NEW_FILE_RE.search(section):
            # 新增文件:取 b/ 侧路径
            rel = normalize_rel(match.group(2))
            if rel and rel not in new_files:
                new_files.append(rel)
        else:
            # R2/R3 整改:rename/copy 扩展头都不带 new file mode 行
            # (differ 已加 --no-renames 封 rename;copy 头 git apply 同样接受,
            # 且无需牺牲源文件)——两者的落点都等价于新增文件
            for ext_match in _RENAME_TO_RE.finditer(section):
                rel = normalize_rel(ext_match.group(1))
                if rel and rel not in new_files:
                    new_files.append(rel)
            for ext_match in _COPY_TO_RE.finditer(section):
                rel = normalize_rel(ext_match.group(1))
                if rel and rel not in new_files:
                    new_files.append(rel)
    return new_files


def parse_new_symlinks(diff_text: str) -> list[str]:
    """从 unified diff 提取"新建软链"段(new file mode 120000)的相对路径,保序去重。

    R3-Q4 实证:合法落点新建 120000 软链此前静态门禁/落点校验/git apply 三关
    都不拦,Linux 容器内 verify 阶段可 import 工作区外模块——门禁层收口为
    "一律拒",与链接目标无关(目标写在文件内容里,静态文本无从审计其合法性)。
    """
    links: list[str] = []
    matches = list(_DIFF_GIT_RE.finditer(diff_text))
    for i, match in enumerate(matches):
        section_start = match.end()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        section = diff_text[section_start:section_end]
        mode = _NEW_FILE_MODE_RE.search(section)
        if mode is not None and mode.group(1) == _SYMLINK_MODE:
            rel = normalize_rel(match.group(2))
            if rel and rel not in links:
                links.append(rel)
    return links


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
        # 3. 路径门禁:穿越/绝对路径/.git 内部文件;以及 allowed_paths 白名单
        # 冒号只按 Windows 盘符形态拒绝(复盘 P2:C:);POSIX 合法文件名
        # 如 weird:name.py 不再被误判为越界
        parts = normalize_rel(rel).split("/")
        if (
            ".." in parts
            or ".git" in parts
            or rel.startswith(("/", "\\"))
            or re.fullmatch(r"[A-Za-z]:", parts[0])
        ):
            violations.append(GateViolation("paths", f"path escapes workspace: {rel}"))
            continue
        if not path_allowed(rel, allowed_paths):
            violations.append(GateViolation("paths", f"path outside allowed scope: {rel}"))

    # 2.5 影子门禁(N-1):新增文件不得与解释器/工具链顶层模块同名,
    # 否则 python -m pytest 的 sys.path[0]=cwd 会让影子包劫持整个判定层。
    # 顶层段可能是包目录(pytest/)或模块文件(os.py/.pyd/.so),剥可导入后缀后比对。
    for rel in parse_new_files(diff_text):
        top = normalize_rel(rel).split("/")[0]
        candidates = {top}
        for suffix in _IMPORTABLE_SUFFIXES:
            if top.endswith(suffix) and len(top) > len(suffix):
                candidates.add(top[: -len(suffix)])
        # PEP 3149 扩展标签链:json.cpython-314-x86_64-linux-gnu.so 可作为
        # 顶层模块 json 导入——扩展链里含可导入后缀时,首段点号前即模块名
        first_dot = top.find(".")
        if first_dot > 0 and any(s in top[first_dot:] for s in _IMPORTABLE_SUFFIXES):
            candidates.add(top[:first_dot])
        if any(name in _RESERVED_TOP_LEVEL for name in candidates):
            violations.append(
                GateViolation("shadow", f"new top-level module shadows toolchain: {rel}")
            )

    # 2.6 软链门禁(P3-6/R3-Q4 收口):new file mode 120000 一律拒——允许新建软链
    # 等于把"工作区外文件"接进工作区(verify 阶段可被 import/读),而链接目标的
    # 合法性是文件内容,静态文本无从审计,故与目标无关一律拒绝。
    violations.extend(
        GateViolation("files", f"new symlink (mode 120000) is forbidden: {rel}")
        for rel in parse_new_symlinks(diff_text)
    )

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
    deadline_epoch: float | None = None,
) -> None:
    """第 6 项(资源门禁):轮数/token/时间任一超限即 BudgetError。

    时间口径(S02b,ADR-0009 §3):deadline_epoch 是**持久化的墙钟截止时刻**
    (执行启动时建立、进 checkpoint、恢复沿用原值的剩余时间),不再依赖
    进程内 monotonic 起点——monotonic 跨进程无意义,恢复会因此重授 900s。
    """
    if round_no > max_rounds:
        raise BudgetError(f"round {round_no} exceeds max_rounds={max_rounds}")
    if token_budget > 0 and tokens_used > token_budget:
        raise BudgetError(f"tokens {tokens_used} exceed budget {token_budget}")
    if deadline_epoch is not None and time.time() > deadline_epoch:
        raise BudgetError("task exceeded time budget (deadline passed)")
