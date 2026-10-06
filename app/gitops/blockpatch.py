r"""apply_patch 块协议:解析器与编译器(Codex 风格)。

模型只见到块文法(`*** Begin/End Patch` + Update/Add/Delete 段 + 空格/`-`/`+` 行),
unified diff 仍是系统内唯一的事实源:本模块把块编译成带真实行号的 unified diff,
之后的防线链(静态门禁 → 落点校验 → `git apply --check` → 快照 → apply → ast 预检)
一个字节都不改。行号由编译器算出,模型写错行号这一整类失败模式在协议层消失。

文法(与 `app/prompts.py` 内嵌示例同形):

    *** Begin Patch
    *** Update File: src/dateparse.py
    @@ def parse_date(value):
         if value is None:
             return None
    +    if not value.strip():
    +        return None
    *** Add File: src/util.py
    +def clamp(v, lo, hi):
    +    return max(lo, min(hi, v))
    *** Delete File: src/legacy_hook.py
    *** End Patch

约定:`@@` 起始行是锚点提示/分块边界,内容一律忽略;`\` 起始行是转义上下文行
(剥掉首个 `\` 即真实内容),用于内容本身以 `*`、`+`、`-`、空格、`\` 开头的情形;
裸 `\ No newline at end of file` 是"上一内容行无行尾符"标记。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from app.tools.paths import normalize_rel, relpath_within

BEGIN = "*** Begin Patch"
END = "*** End Patch"
_HEADERS = {"*** Update File:": "update", "*** Add File:": "add", "*** Delete File:": "delete"}
_NO_EOL = "\\ No newline at end of file"
_NO_EOL_KEY = "\ue000noeol"

__all__ = [
    "BEGIN",
    "END",
    "BlockPatchError",
    "BlockSection",
    "compile_block_patch",
    "parse_block_patch",
    "unified_to_block",
]


@dataclass
class BlockSection:
    """一个段:动作、归一化后的仓库内相对路径、去段头后的原始 body 行。"""

    action: str  # "add" | "update" | "delete"
    path: str
    lines: list[str] = field(default_factory=list)


class BlockPatchError(Exception):
    """协议级拒绝。tag 复用门禁词表(format/paths),reason 是细类,detail 供模型修正。"""

    def __init__(self, tag: str, reason: str, detail: str) -> None:
        super().__init__(f"[{tag}] {reason}: {detail}")
        self.tag = tag
        self.reason = reason
        self.detail = detail


def _bad_header(detail: str) -> BlockPatchError:
    return BlockPatchError("format", "bad_header", detail)


def split_keepends(text: str) -> list[str]:
    """按 \\n 切行并保留行尾符。

    禁用 `str.splitlines()`:它还会在 \\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028 处切行,
    会把前像切碎;也禁用 `Path.read_text`(universal-newlines 把 CRLF 静默变 LF)。
    """
    parts = text.split("\n")
    last = parts.pop()
    lines = [p + "\n" for p in parts]
    if last:
        lines.append(last)
    return lines


def _check_section_path(rel: str) -> str:
    """段头路径预检(fail-fast 提前拒;门禁与落点校验照旧复查,双防线)。

    必须先归一再判:`normalize_rel` 只做反斜杠→正斜杠、剥 `./` 前缀、剥尾 `/`,
    刻意不吞 `..`/绝对路径(P0-2:用 lstrip("./") 会让门禁的越界分支永不命中)。
    """
    norm = normalize_rel(rel)
    if not norm:
        raise BlockPatchError("format", "missing_path", "段头缺少文件路径")
    parts = norm.split("/")
    if (
        ".." in parts
        or ".git" in parts
        or norm.startswith(("/", "\\"))
        or norm.startswith("~")
        or ":" in parts[0]
    ):
        raise BlockPatchError("paths", "path_escape", f"路径必须落在仓库内且不在 .git 下:{rel}")
    return norm


def _strip_fences(lines: list[str]) -> list[str]:
    """容忍模型把补丁包在 markdown 围栏里。"""
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return lines


def _parse_header(line: str) -> tuple[str, str]:
    for prefix, action in _HEADERS.items():
        if line.startswith(prefix):
            return action, _check_section_path(line[len(prefix) :].strip())
    if line.strip() == END:
        raise _bad_header(f"{END} 之后仍有内容")
    raise _bad_header(f"无法识别的段头行:{line}")


def _check_body(section: BlockSection) -> None:
    """body 行前缀合法性(按段动作)。"""
    for raw in section.lines:
        if raw == _NO_EOL:
            continue
        if section.action == "add":
            if not raw.startswith(("+", "\\")):
                raise _bad_header(f"Add File 段只允许 + 行:{raw}")
        elif section.action == "delete":
            raise BlockPatchError(
                "format", "delete_body_not_empty", f"Delete File 段不允许任何内容行:{raw}"
            )
        elif not raw.startswith((" ", "-", "+", "@", "\\")) and raw != "":
            raise _bad_header(f"Update File 段行前缀必须是空格/-/+/@@/\\:{raw}")


def parse_block_patch(text: str) -> list[BlockSection]:
    """块文本 → 段列表;文法不合即 BlockPatchError。"""
    if not text.strip():
        raise BlockPatchError("format", "empty_patch", "补丁文本为空")
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    lines = _strip_fences(lines)
    if not lines or lines[0].strip() != BEGIN:
        raise _bad_header(f"首行必须是 {BEGIN},实际:{lines[0] if lines else '(空)'}")
    if lines[-1].strip() != END:
        raise BlockPatchError("format", "unbalanced", f"缺少结束行 {END}")

    sections: list[BlockSection] = []
    seen: set[str] = set()
    current: BlockSection | None = None
    for raw in lines[1:-1]:
        if raw.startswith("***"):
            if raw.strip() == BEGIN:
                raise _bad_header(f"嵌套的 {BEGIN}")
            action, path = _parse_header(raw)
            if path in seen:
                raise BlockPatchError("format", "duplicate_path", f"同一路径出现两个段:{path}")
            seen.add(path)
            current = BlockSection(action=action, path=path)
            sections.append(current)
            continue
        if current is None:
            raise _bad_header(f"段头之前出现内容:{raw}")
        current.lines.append(raw)

    if not sections:
        raise BlockPatchError("format", "empty_patch", "补丁不含任何文件段")
    for section in sections:
        _check_body(section)
    return sections


def _workspace_target(workspace: Path, rel: str) -> Path:
    """段路径 → 工作区内绝对路径;编译器只经此读文件(读侧越界即拒)。"""
    target = relpath_within(workspace, rel)
    if target is None:
        raise BlockPatchError("paths", "path_escape", f"路径落在工作区外:{rel}")
    return target


def _read_lines(target: Path, rel: str) -> list[str]:
    """按字节读并自行切行:保留每行真实行尾(CRLF 与无尾换行都不能被"归一")。"""
    if not target.is_file():
        raise BlockPatchError("context", "target_missing", f"目标文件不存在:{rel}")
    data = target.read_bytes()
    if b"\x00" in data[:1024]:
        raise BlockPatchError("format", "binary_target", f"{rel} 是二进制文件,块协议不处理")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlockPatchError(
            "format", "encoding_unsupported", f"{rel} 不是 UTF-8 文本:{exc}"
        ) from exc
    return split_keepends(text)


def _strip_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _eol(line: str) -> str:
    return line[len(_strip_eol(line)) :]


def _key(line: str) -> str:
    r"""hunk 生成的比对键:无行尾符的末行加哨兵,渲染时据此出 `\ No newline...`。"""
    return line if line.endswith("\n") else line + _NO_EOL_KEY


def _emit(op: str, lines: list[str]) -> list[str]:
    """diff 行 = 前缀 + 该行原始字节(含其自身行尾);无行尾的末行补 no-eol 标记。

    标记必须独立成行:git 的形态是 `-b\\n\\ No newline at end of file\\n`,把标记粘在
    内容行尾会让 `git apply` 判为 corrupt patch。
    """
    out: list[str] = []
    for line in lines:
        piece = op + line
        no_eol = not line.endswith("\n")
        out.append(piece + "\n" if no_eol else piece)
        if no_eol:
            out.append(_NO_EOL + "\n")
    return out


def _update_chunks(lines: list[str]) -> list[tuple[list[str], list[str]]]:
    r"""Update 段 body → [(old 内容, new 内容)];`@@` 起始行分块。

    块协议里 body 行的首个字符是操作符,内容一律剥掉操作符后再比;`\` 起始行是
    转义上下文行(剥一个 `\` 即真实内容);空行按单空格上下文行处理。
    """
    chunks: list[tuple[list[str], list[str]]] = []
    old: list[str] = []
    new: list[str] = []

    def _close() -> None:
        nonlocal old, new
        if old or new:
            chunks.append((old, new))
        old, new = [], []

    for raw in lines:
        if raw.startswith("@@"):
            _close()
            continue
        if raw == _NO_EOL:
            continue
        if raw == "":
            old.append("")
            new.append("")
            continue
        if raw.startswith("\\"):
            old.append(raw[1:])
            new.append(raw[1:])
            continue
        op, content = raw[0], raw[1:]
        if op in (" ", "-"):
            old.append(content)
        if op in (" ", "+"):
            new.append(content)
    _close()
    if not chunks:
        raise BlockPatchError("format", "empty_section", "Update File 段没有任何内容行")
    return chunks


def _locate(keys: list[str], want: list[str], cursor: int, rel: str) -> int:
    """在磁盘前像里找 old 块的唯一命中位;0 处/多处/早于游标分别拒。"""
    if not want:
        return cursor
    hits = [i for i in range(len(keys) - len(want) + 1) if keys[i : i + len(want)] == want]
    if not hits:
        raise BlockPatchError(
            "context",
            "anchor_not_found",
            f"{rel} 里找不到该上下文块;首行内容:{want[0]!r}——上下文行必须与文件逐字一致",
        )
    if len(hits) > 1:
        raise BlockPatchError(
            "context",
            "ambiguous_anchor",
            f"{rel}:该上下文在文件中出现 {len(hits)} 处,请在块首尾补充更多上下文行使其唯一",
        )
    pos = hits[0]
    if pos < cursor:
        raise BlockPatchError(
            "context",
            "chunks_out_of_order",
            f"{rel}:块内的多个 @@ 段必须按文件中的先后顺序书写",
        )
    return pos


def _terminators(pre: list[str], pos: int, width: int) -> tuple[str, str]:
    """新行行尾:(普通行行尾, 末行行尾)。

    普通行沿用被匹配旧块里第一个真实行尾;末行沿用旧块末行的行尾状态(计划书规则)。
    旧块为空(零宽插入)时取插入点之后/之前那行的行尾——绝不把"文件末行无行尾符"
    的状态传染给整段新行,否则新行会被粘成一行。
    """
    terms = [_eol(line) for line in pre[pos : pos + width]]
    real = next((t for t in terms if t), "")
    if not terms:
        after = _eol(pre[pos]) if pos < len(pre) else ""
        before = _eol(pre[pos - 1]) if pos > 0 and pre else ""
        real = after or before or "\n"
    if not real:
        real = "\n"
    last = terms[-1] if terms and terms[-1] else real
    return real, last


def _compile_update(section: BlockSection, target: Path) -> str:
    pre = _read_lines(target, section.path)
    keys = [_strip_eol(line) for line in pre]
    post = list(pre)
    cursor = 0
    delta = 0
    for old, new in _update_chunks(section.lines):
        pos = _locate(keys, old, cursor, section.path)
        real_term, last_term = _terminators(pre, pos, len(old))
        replaced = [content + real_term for content in new[:-1]]
        if new:
            replaced.append(new[-1] + last_term)
        start = pos + delta
        post[start : start + len(old)] = replaced
        delta += len(replaced) - len(old)
        cursor = pos + len(old)
    if post == pre:
        raise BlockPatchError(
            "format", "empty_section", f"{section.path}:块只含上下文行,没有任何改动"
        )

    pre_keys = [_key(line) for line in pre]
    post_keys = [_key(line) for line in post]
    body: list[str] = []
    for group in SequenceMatcher(None, pre_keys, post_keys).get_grouped_opcodes(3):
        old_count = sum(i2 - i1 for _, i1, i2, _, _ in group)
        new_count = sum(j2 - j1 for _, _, _, j1, j2 in group)
        body.append(f"@@ -{group[0][1] + 1},{old_count} +{group[0][3] + 1},{new_count} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag in ("replace", "delete"):
                body.extend(_emit("-", pre[i1:i2]))
            if tag in ("replace", "insert"):
                body.extend(_emit("+", post[j1:j2]))
            if tag == "equal":
                body.extend(_emit(" ", pre[i1:i2]))
    path = section.path
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n" + "".join(body)


def _body_contents(lines: list[str]) -> list[str]:
    r"""Add 段 body → 真实内容行(`+` 与 `\` 转义行都剥首字符)。"""
    return [raw[1:] for raw in lines if raw and raw != _NO_EOL]


def _compile_add(section: BlockSection, target: Path) -> str:
    if target.exists():
        raise BlockPatchError(
            "context", "add_exists", f"新增目标已存在:{section.path}(改内容请用 Update File)"
        )
    contents = _body_contents(section.lines)
    header = f"diff --git a/{section.path} b/{section.path}\nnew file mode 100644\n"
    header += f"--- /dev/null\n+++ b/{section.path}\n"
    if not contents:
        # 空新文件:只出段头。git apply 拒绝 `@@ -0,0 +1,0 @@` 形态的空 hunk。
        return header
    no_eol = bool(section.lines) and section.lines[-1] == _NO_EOL
    file_lines = [
        content + ("" if no_eol and i == len(contents) - 1 else "\n")
        for i, content in enumerate(contents)
    ]
    body = [f"@@ -0,0 +1,{len(contents)} @@\n", *_emit("+", file_lines)]
    return header + "".join(body)


def _compile_delete(section: BlockSection, target: Path) -> str:
    if not target.is_file():
        raise BlockPatchError("context", "delete_missing", f"待删除的文件不存在:{section.path}")
    pre = _read_lines(target, section.path)
    header = f"diff --git a/{section.path} b/{section.path}\ndeleted file mode 100644\n"
    header += f"--- a/{section.path}\n+++ /dev/null\n"
    if not pre:
        return header
    body = [f"@@ -1,{len(pre)} +0,0 @@\n", *_emit("-", pre)]
    return header + "".join(body)


def compile_block_patch(workspace: Path | str, sections: list[BlockSection]) -> str:
    """块段 → unified diff(内部唯一事实源)。

    每段都自带 `diff --git` 头:门禁的 parse_new_files/parse_new_symlinks 只扫
    `diff --git` 段,缺它则影子门禁与软链门禁对新增文件整体失明。不写 `index` 行
    (git apply 从 stdin 读取时不需要)。
    """
    ws = Path(workspace)
    out: list[str] = []
    for section in sections:
        target = _workspace_target(ws, section.path)
        if section.action == "add":
            out.append(_compile_add(section, target))
        elif section.action == "delete":
            out.append(_compile_delete(section, target))
        else:
            out.append(_compile_update(section, target))
    return "".join(out)


def _side_path(raw: str) -> str:
    """`a/src/x.py` / `b/src/x.py` / `/dev/null` → 仓库内相对路径或 /dev/null。"""
    value = raw.split("\t")[0].strip().strip('"')
    if value == "/dev/null":
        return "/dev/null"
    return normalize_rel(value[2:] if value[:2] in ("a/", "b/") else value)


def _has(lines: list[str], prefix: str) -> bool:
    return any(ln.startswith(prefix) for ln in lines)


def _record_action(lines: list[str]) -> tuple[str, str]:
    """一段原始 diff → (action, path)。"""
    if (
        _has(lines, "rename from ")
        or _has(lines, "rename to ")
        or _has(lines, "copy from ")
        or _has(lines, "copy to ")
    ):
        raise BlockPatchError("format", "bad_header", "块协议不迁移 rename/copy 段")
    if _has(lines, "GIT binary patch") or _has(lines, "Binary files "):
        raise BlockPatchError("format", "bad_header", "块协议不迁移二进制 diff")
    from_side = next((ln[4:] for ln in lines if ln.startswith("--- ")), "")
    to_side = next((ln[4:] for ln in lines if ln.startswith("+++ ")), "")
    if not from_side and not to_side:
        raise BlockPatchError("format", "bad_header", "diff 段缺少 --- / +++ 头")
    if from_side.split("\t")[0].strip() == "/dev/null" or _has(lines, "new file mode "):
        return "add", _side_path(to_side)
    if to_side.split("\t")[0].strip() == "/dev/null" or _has(lines, "deleted file mode "):
        return "delete", _side_path(from_side)
    return "update", _side_path(to_side)


def _split_records(diff_text: str) -> list[list[str]]:
    """unified diff → 每文件一段的原始行。

    边界 = `diff --git` 头,或"其后紧跟 `+++ ` 头"的 `--- ` 行(现存语料大多没有
    `diff --git`,只能靠 `---`/`+++` 配对定位;加后继判定是为了不把内容恰好以
    `-- ` 开头的删除行误认成段头)。
    """
    lines = diff_text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    records: list[list[str]] = []
    cur: list[str] = []
    for i, raw in enumerate(lines):
        boundary = raw.startswith("diff --git ") or (
            raw.startswith("--- ") and any(n.startswith("+++ ") for n in lines[i + 1 : i + 3])
        )
        if boundary and _has(cur, "+++ "):
            records.append(cur)
            cur = []
        cur.append(raw)
    if cur:
        records.append(cur)
    return records


def unified_to_block(diff_text: str) -> str:
    r"""unified diff → 块文本(迁移工具:回放语料与测试内嵌 diff 共用)。

    与 parse/compile 互为 round-trip:同一文件的多 hunk 之间输出一行裸 `@@` 作分块
    边界,编译时按整文件前像/后像重算 hunk,故承诺是"应用结果逐字节等价"而非
    "diff 文本逐字相等"(未变动区 ≥7 行才分成两 hunk,git apply 两种都接受)。
    """
    if not diff_text.strip():
        raise BlockPatchError("format", "empty_patch", "没有可转换的 diff")
    out: list[str] = [BEGIN]
    for lines in _split_records(diff_text):
        if not (_has(lines, "--- ") or _has(lines, "+++ ") or _has(lines, "diff --git ")):
            continue
        action, path = _record_action(lines)
        head = max(
            (i for i, ln in enumerate(lines) if ln.startswith(("--- ", "+++ "))),
            default=-1,
        )
        out.append(f"*** {action.capitalize()} File: {path}")
        if action == "delete":
            continue
        hunk = 0
        for raw in lines[head + 1 :]:
            if raw.startswith("@@"):
                hunk += 1
                if hunk > 1:
                    out.append("@@")
                continue
            if action == "add" and not raw.startswith(("+", "\\", _NO_EOL)):
                raise _bad_header(f"新增段出现非 + 行:{raw}")
            out.append(raw)
    if len(out) == 1:
        raise BlockPatchError("format", "bad_header", "diff 里没有可转换的文件段")
    out.append(END)
    return "\n".join(out) + "\n"
