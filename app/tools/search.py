"""检索引擎:`search_code` 的加速路径与扫描路径,输出必须与旧实现逐字等价。

规格红线(M3 / PROGRESS.md D.6):默认参数下存量测试与两臂消融对照都建立在旧口径上,
所以两边必须给出同样的 `{path, line, text}` 条目、同样的"文件序 + 行序"排序、同样的
200 字符裁剪、同样的 `max_results` 取前 N 与 `truncated` 判定——包括旧 `files.py` 那条
"命中数刚好达上限时,按后继文件位置判定 truncated"的口径(见 `_cap_matches`:它复刻的是
循环在下一个文件的循环头 break 这一事实,不是"存在第 N+1 条匹配")。

职责划分(本模块的核心设计,不是实现细节):rg 只回答**"哪些文件可能含有这个关键词"**
(一次 `-l`,不读进 Python 逐行小写化),行级的文本、行号、上下文与裁剪**只有一条 Python
实现**。差分探针(`tests/test_search_tools.py::test_form_feed_and_bom_and_lone_cr_*`)证明
把 rg 当行级权威会破等价:rg 剥 BOM、只按 `\\n` 断行,而 `read_text().splitlines()` 还按
`\\r`/`\\x0b`/`\\x0c`/`\\x1c`-`\\x1e`/`\\x85`/`\\u2028`/`\\u2029` 断行(`\\x0c` 分页符在真实
Python 源码里并不罕见,行号错一位补丁就贴不上)。改成文件级预筛后,这些差异只影响"快不快"。
残余风险只剩一条且方向是"多送不漏送":字面子串在短行里命中必然在其超串(rg 的长行)里命中,
故 rg 的文件集合是 Python 命中集合的**超集**;唯一例外是非 ASCII 大小写折叠(Python 全量
`lower()` vs Rust 简单 folding),由"预筛报了文件而 Python 一行没命中 → 整仓复扫"兜住。

`regex=True` 一律走 Python:Rust 正则方言 ≠ Python `re`(后视断言直接 rc=2,`\\w` 的 Unicode
语义也有差异),而正则模式正是模型指望"高级语义"的时候,不给第二套答案。

边界:引擎只在调用方给的 `files`(= `app.tools.files._iter_repo_files`,已按 SKIP_DIRS / glob /
500 条上限筛过)里取数,不遍历目录,也没有资格新增文件集合(rg 的报告逐个核回候选集)。
`--no-ignore --hidden` 照常传:rg 默认会按 .gitignore 裁文件、跳过点文件,与旧 `rglob("*")`
口径相反。二进制不在预筛阶段过滤,由行级唯一实现的 `looks_like_text` 定案——rg 多报的成不了
结果,少报也不可能(`--text` 一路读到文件尾)。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.tools.paths import looks_like_text

log = logging.getLogger(__name__)

ENGINE_RG = "rg"
ENGINE_PYTHON = "python"
ENGINE_KEY = "engine"  # meta 里"这条查询实际由谁服务"的键,由 search_code 透进工具输出
RG_BINARY = "rg"
RG_TIMEOUT_SECONDS = 20.0
TEXT_TRIM_CHARS = 200

# rg 退出码:0=有匹配文件,1=无匹配(这是合法空结果,不是失败),其余(含 2=参数/读盘错误)按失败回退
_RG_OK_EXITS = (0, 1)
# 每批参数的字符预算:Windows CreateProcess 上限 32767,留出引号与前缀余量;超了就多切一批
_RG_ARGV_LIMIT = 24_000

_MATCHER = Callable[[str], bool]


def literal_matcher(needle: str) -> _MATCHER:
    """默认口径:大小写不敏感的**字面子串**,与迁移前 `needle in line.lower()` 同一条表达式。"""
    lowered = needle.lower()
    return lambda line: lowered in line.lower()


def compile_pattern(needle: str) -> re.Pattern[str]:
    """`regex=True` 的唯一编译口径(IGNORECASE);校验与扫描共用,避免两处标志漂移。"""
    return re.compile(needle, re.IGNORECASE)


def regex_matcher(pattern: re.Pattern[str]) -> _MATCHER:
    return lambda line: pattern.search(line) is not None


def _rg_binary() -> str | None:
    """rg 二进制的唯一取用点(也是"这台机器有没有加速可用"的唯一判据)。"""
    return shutil.which(RG_BINARY)


def resolve_engine(prefer: str) -> str:
    """`auto`/`rg` → 找到 rg 二进制才走加速;`python` → 恒走纯 Python(即便装了 rg)。"""
    if prefer == ENGINE_PYTHON:
        return ENGINE_PYTHON
    return ENGINE_RG if _rg_binary() else ENGINE_PYTHON


def grep_files(
    workspace: Path,
    needle: str,
    *,
    files: Sequence[str],
    max_results: int = 50,
    context_lines: int = 0,
    per_file_cap: int = 0,
    regex: bool = False,
    engine: str = "auto",
    meta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """在 `files`(仓库内相对 POSIX 路径,顺序即扫描顺序)里检索 `needle`,取前 N 条。

    返回 (matches, truncated)。`meta` 是出参:加速是否生效写进 `meta["engine"]`
    (行级扫描永远是 Python,所以这个键读作"文件集合是谁给的"),
    回落原因写进 `meta["fallback"]`(调用方落日志,模型只看 engine)。
    """
    sink = meta if meta is not None else {}
    listed = list(files)
    matcher = regex_matcher(compile_pattern(needle)) if regex else literal_matcher(needle)
    scan_files = listed
    label = ENGINE_PYTHON

    if resolve_engine(engine) == ENGINE_RG and not regex:
        prefetched = _rg_files(
            _rg_binary() or RG_BINARY, workspace, needle, files=listed, meta=sink
        )
        if prefetched is not None:
            scan_files = prefetched
            label = ENGINE_RG
    elif regex:
        sink["fallback"] = "regex mode is answered by the Python path (Rust regex ≠ Python re)"

    collected = _python_scan(
        workspace,
        scan_files,
        matcher,
        max_results=max_results,
        context_lines=context_lines,
        per_file_cap=per_file_cap,
    )
    if label == ENGINE_RG and scan_files and not collected:
        # rg 说这些文件里有、Python 一行都没命中:两侧的词法/折叠口径在打架,
        # 不能拿"空手"当结论 → 整仓复扫。宁可慢,不可少给模型证据
        log.debug("search: rg 预筛与 Python 扫描不一致,整仓复扫 workspace=%s", workspace)
        collected = _python_scan(
            workspace,
            listed,
            matcher,
            max_results=max_results,
            context_lines=context_lines,
            per_file_cap=per_file_cap,
        )
        label = ENGINE_PYTHON
        sink["fallback"] = "rg prefilter produced no line-level match; full Python rescan"
    sink[ENGINE_KEY] = label
    # 截断判定用**完整** files:旧循环的 truncated 取决于"命中文件之后还有没有文件",
    # 与 rg 给了几个候选无关(用子集会算错那个反直觉口径)
    return _cap_matches(collected, listed, max_results)


def _cap_matches(
    collected: Sequence[dict[str, Any]], files: Sequence[str], max_results: int
) -> tuple[list[dict[str, Any]], bool]:
    """唯一的"取前 N + 判定 truncated"实现(口径见模块头的第一段)。"""
    if max_results <= 0:
        # 旧口径:上限为 0 时循环在第一个文件的循环头就 break,只要 files 非空即报截断
        return [], bool(files)
    if len(collected) > max_results:
        return list(collected[:max_results]), True
    kept = list(collected)
    if not kept:
        return kept, False
    position = {rel: index for index, rel in enumerate(files)}[kept[-1]["path"]]
    return kept, position < len(files) - 1  # 刚好达上限:其后还有文件 → 旧循环会 break


def _trim(line: str) -> str:
    return line.strip()[:TEXT_TRIM_CHARS]


def _entry(rel: str, index: int, lines: Sequence[str], context_lines: int) -> dict[str, Any]:
    """一条命中:`{path, line, text}`;`context_lines>0` 才追加 before/after(默认不出这两个键)。"""
    entry: dict[str, Any] = {"path": rel, "line": index + 1, "text": _trim(lines[index])}
    if context_lines > 0:
        entry["before"] = [_trim(item) for item in lines[max(0, index - context_lines) : index]]
        entry["after"] = [_trim(item) for item in lines[index + 1 : index + 1 + context_lines]]
    return entry


def _per_file_limit(max_results: int, per_file_cap: int) -> int:
    """单文件最多收集几条:显式上限优先,否则"全局上限 + 1"(多收一条只用于判定 truncated)。"""
    return per_file_cap if per_file_cap > 0 else max_results + 1


def _python_scan(
    workspace: Path,
    files: Sequence[str],
    matcher: _MATCHER,
    *,
    max_results: int,
    context_lines: int,
    per_file_cap: int,
) -> list[dict[str, Any]]:
    """行级权威实现:逐文件读全文、逐行匹配、按 (文件序, 行号) 产出命中。

    二进制(前 1KB 有 NUL)与读盘失败跳过;收集到 `max_results + 1` 条即停
    (多余那条只用于判定 truncated),不做无谓的全仓穷举。
    """
    limit = _per_file_limit(max_results, per_file_cap)
    out: list[dict[str, Any]] = []
    for rel in files:
        path = workspace / rel
        if not looks_like_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        counted = 0
        for index, line in enumerate(lines):
            if not matcher(line):
                continue
            if counted >= limit:
                break  # 单文件上限:命中之后的匹配行不再计入(per_file_cap=0 时永不触发)
            counted += 1
            out.append(_entry(rel, index, lines, context_lines))
            if len(out) > max_results:
                return out
    return out


def _rg_command(rg_path: str, needle: str, targets: Sequence[str]) -> list[str]:
    """rg 参数表(shell=False;`-l` 只列文件名,不打印内容,故无需 JSON 解析)。

    - `--no-ignore --hidden`:rg 默认按 .gitignore 裁文件并跳过点文件,而旧 `rglob("*")`
      两者都看得见——不传就会被 ignore 的文件会从结果里凭空消失;扫描集合由显式 `targets`
      决定,这两个开关只是"就算 rg 的规则不一样也拦不住我们";
    - `--text`:正文中部有 NUL 的文件 rg 默认在 NUL 处停止(少给文件),Python 读得到全文;
      真二进制不靠 rg 的二进制识别排除,由 `_python_scan` 的 `looks_like_text` 定案;
    - `--files-with-matches`:只要文件级答案,某文件命中一处即停在其上;
    - `--fixed-strings` + `-e`:字面匹配,且关键词以 `-` 开头时不会被当成选项
      (rg 的长选项叫 `--regexp`;`-i` 写成 `--case-insensitive`、`-e` 写成 `--expression`
      都会 rc=2 全量回落——两个坑都踩过,故这里不换写法)。
    """
    return [
        rg_path,
        "--files-with-matches",
        "--text",
        "--hidden",
        "--no-ignore",
        "-i",
        "--fixed-strings",
        "-e",
        needle,
        "--",
        *targets,
    ]


def _run_rg(cmd: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    """唯一的 subprocess 出口:shell=False、有界超时(超时由 run() 负责 kill)、UTF-8 解码。"""
    return subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RG_TIMEOUT_SECONDS,
        shell=False,
    )


def _rg_files(
    rg_path: str,
    workspace: Path,
    needle: str,
    *,
    files: list[str],
    meta: dict[str, Any],
) -> list[str] | None:
    """rg 文件级预筛:返回"可能含有 needle"的文件子集(保持 `files` 里的原序),None=这条路径没跑成。

    显式文件参数(而不是目录遍历)是刻意的:遍历会把 SKIP_DIRS 之外的产物目录
    (`runs/`、`.pytest-tmp*`、目标仓库的 build 目录)整个走一遍——实测在这台机器上
    一次遍历就撞上 20s 超时,比旧实现还慢。参数表按 `_RG_ARGV_LIMIT` 分批,
    每批一次 subprocess;任何一批跑不成,整个预筛作废(宁可慢,不冒半份结果的风险)。
    rg 打印的路径就是我们传进去的相对路径(实测逐字符回显),但仍按"必须在候选集里"
    再核一遍:加速路径永远没有资格**新增**文件集合。
    """
    if any((workspace / rel).is_symlink() for rel in files):
        # 符号链接目标可能在 workspace 外,而 rg 对显式参数的跟随语义无契约可依:
        # 整批交给 Python 路径(它与今日逐字相同地 read_text),既不放宽边界也不换口径
        meta["fallback"] = "scope contains symlink"
        return None
    allowed = set(files)
    hits: set[str] = set()
    for batch in _arg_batches(rg_path, needle, files):
        try:
            proc = _run_rg(_rg_command(rg_path, needle, batch), workspace)
        except subprocess.TimeoutExpired:
            meta["fallback"] = f"timeout after {RG_TIMEOUT_SECONDS}s"
            return None
        except OSError as exc:
            meta["fallback"] = f"spawn failed: {exc}"
            return None
        if proc.returncode not in _RG_OK_EXITS:
            meta["fallback"] = f"exit {proc.returncode}: {(proc.stderr or '')[:160]}"
            return None
        for line in (proc.stdout or "").splitlines():
            rel = line.strip().replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if rel in allowed:
                hits.add(rel)
    return [rel for rel in files if rel in hits]


def _arg_batches(rg_path: str, needle: str, files: Sequence[str]) -> list[list[str]]:
    """把候选文件切成若干批,每批的参数表长度都留在安全预算内。

    预算不是装饰:Windows 的 CreateProcess 命令行上限是 32767 字符,且 list2cmdline
    会给含空格的参数再加引号;放不下时**不许**截断参数表(那会静默少文件),
    多切一批就多一次 subprocess,语义不变。
    """
    fixed = len(rg_path) + len(needle) + 48  # 固定开关与分隔符的粗估
    batches: list[list[str]] = []
    current: list[str] = []
    used = fixed
    for rel in files:
        cost = len(rel) + 3
        if current and used + cost > _RG_ARGV_LIMIT:
            batches.append(current)
            current, used = [], fixed
        current.append(rel)
        used += cost
    if current:
        batches.append(current)
    return batches
