"""路径与文件安全规则:工具层与门禁共用的唯一实现。"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath

TEST_DIR_SEGMENTS = {"tests", "test"}
TEST_FILE_PREFIXES = ("test_",)
TEST_FILE_SUFFIXES = ("_test.py",)


def is_test_file(rel_path: str) -> bool:
    """判断仓库内相对路径是否属于测试文件(禁止 Agent 修改)。"""
    p = PurePosixPath(rel_path.replace("\\", "/"))
    if any(seg.lower() in TEST_DIR_SEGMENTS for seg in p.parts[:-1]):
        return True
    name = p.name.lower()
    return name.startswith(TEST_FILE_PREFIXES) or name.endswith(TEST_FILE_SUFFIXES)


def normalize_rel(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("./").rstrip("/")


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
    """变更路径是否落在允许范围内;allowed_patterns 为空/None 表示不限制。"""
    if not allowed_patterns:
        return True
    norm = normalize_rel(rel_path)
    return any(fnmatch.fnmatch(norm, normalize_rel(pat)) for pat in allowed_patterns)


def looks_like_text(path: Path) -> bool:
    """前 1KB 出现 NUL 字节即视为二进制。"""
    try:
        with path.open("rb") as fh:
            return b"\x00" not in fh.read(1024)
    except OSError:
        return False
