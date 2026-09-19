"""路径与文件安全规则:工具层与门禁共用的唯一实现。"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

TEST_DIR_SEGMENTS = {"tests", "test"}
TEST_FILE_PREFIXES = ("test_",)
TEST_FILE_SUFFIXES = ("_test.py",)
# pytest 控制面文件:改它们即可改变测试的收集/跳过行为,与改测试同罪(P0-1②)
PYTEST_CONTROL_FILES = frozenset(
    {"conftest.py", "pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml"}
)
# 证据链控制面文件(N-6):`git add -A -N` 会跳过 ignored 路径,改 .gitignore 即可
# 把任意新增文件藏出 diff/changed_files/diff.patch——判定与取证以 git diff 视角为准,
# 故与 pytest 控制面同罪禁改。
EVIDENCE_CONTROL_FILES = frozenset({".gitignore", ".gitattributes"})


def is_test_file(rel_path: str) -> bool:
    """判断仓库内相对路径是否属于测试文件(禁止 Agent 修改)。

    测试目录/test_ 命名之外,pytest 的控制面文件(conftest/ini/cfg/tox/pyproject)
    能决定"哪些测试被收集、是否被跳过";.gitignore/.gitattributes 能把文件藏出
    diff 视角——三者都是"改一个文件、改写整个判定",门禁一并禁改。
    """
    p = PurePosixPath(rel_path.replace("\\", "/"))
    if any(seg.lower() in TEST_DIR_SEGMENTS for seg in p.parts[:-1]):
        return True
    name = p.name.lower()
    if name in PYTEST_CONTROL_FILES or name in EVIDENCE_CONTROL_FILES:
        return True
    return name.startswith(TEST_FILE_PREFIXES) or name.endswith(TEST_FILE_SUFFIXES)


_DOT_SLASH_PREFIX = re.compile(r"^(?:\./)+")


def normalize_rel(rel_path: str) -> str:
    """归一化为仓库内相对路径:反斜杠转正斜杠,只剥掉真实存在的 "./" 前缀。

    禁止用 lstrip("./"):它按字符集合剥光开头所有 "." 与 "/",会把
    "../../x" 归一成 "x"、"/etc/x" 归一成 "etc/x"、".git/config" 归一成
    "git/config"——gates 的 ".." 与绝对路径分支将永不命中(P0-2)。
    """
    posix = rel_path.replace("\\", "/")
    posix = _DOT_SLASH_PREFIX.sub("", posix)
    return posix.rstrip("/")


def relpath_within(workspace: Path, rel_path: str) -> Path | None:
    """把仓库内相对路径解析为绝对路径;越界(绝对路径/.. 穿越)返回 None。

    绝对路径判断必须走 Windows 语义(盘符、反斜杠、正斜杠盘符路径都算),
    否则 `D:\\repo\\file.py` 会被当作相对路径拼进 workspace 后命中真实文件。
    """
    if not rel_path:
        return None
    if Path(rel_path).is_absolute():
        return None
    posix = PurePosixPath(rel_path.replace("\\", "/"))
    if posix.is_absolute() or rel_path.startswith("~"):
        return None

    candidate = (workspace / posix.as_posix()).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        return None
    return candidate


def path_allowed(rel_path: str, allowed_patterns: list[str] | None) -> bool:
    """变更路径是否落在允许范围内;allowed_patterns 为空/None 表示不限制。

    用 fnmatchcase:fnmatch 在 Windows 上会做大小写归一,同一 manifest 在
    Linux 与 Windows 下会得出不同判定;门禁必须跨平台可复现(P2-6)。
    """
    if not allowed_patterns:
        return True
    norm = normalize_rel(rel_path)
    return any(fnmatch.fnmatchcase(norm, normalize_rel(pat)) for pat in allowed_patterns)


def looks_like_text(path: Path) -> bool:
    """前 1KB 出现 NUL 字节即视为二进制。"""
    try:
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(1024)
    except OSError:
        return False
