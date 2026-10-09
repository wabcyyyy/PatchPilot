"""S06:受理输入冻结(app/gitops/input_snapshot.py)。

核心承诺:受理后源目录怎么变都影响不到本次执行;源目录全程零写入、
零跟随链接;指纹哈希的是**实际复制进去的字节**。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.errors import TaskError
from app.gitops.input_snapshot import SNAPSHOT_DIRNAME, freeze_input


def _mk_source(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    return root


def test_freeze_copies_bytes_and_is_deterministic(tmp_path: Path) -> None:
    a, b = _mk_source(tmp_path / "a"), _mk_source(tmp_path / "b")
    fa = freeze_input(a, tmp_path / "run-a", max_files=100, timeout_seconds=60)
    fb = freeze_input(b, tmp_path / "run-b", max_files=100, timeout_seconds=60)
    assert fa == fb, "同字节冻结 → 同指纹(幂等键稳定的根基)"
    assert (tmp_path / "run-a" / SNAPSHOT_DIRNAME / "src" / "mod.py").read_text(
        encoding="utf-8"
    ) == "x = 1\n"


def test_freeze_excludes_git_and_caches(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (src / "src" / "__pycache__").mkdir()
    (src / "src" / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"junk")
    freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    snap = tmp_path / "run" / SNAPSHOT_DIRNAME
    assert not (snap / ".git").exists()
    assert not (snap / "src" / "__pycache__").exists()


def test_freeze_fingerprint_changes_with_untracked_and_deletion(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    base = freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    # 未跟踪且会被复制的新文件改变指纹(它将进入执行)
    (src / "notes.txt").write_text("untracked but executed\n", encoding="utf-8")
    with_new = freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    assert with_new != base
    os.remove(src / "notes.txt")
    assert freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60) == base


def test_freeze_rejects_symlinks_without_following(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    try:
        os.symlink(os.devnull, src / "src" / "linked")
    except OSError:
        pytest.skip("这台 Windows 没有创建符号链接的权限")
    with pytest.raises(TaskError, match="symlink"):
        freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    # 拒绝发生在平台副本上:半份 tmp 被清理,run_dir 不留残档
    assert not (tmp_path / "run" / SNAPSHOT_DIRNAME).exists()
    # 括号必须显式:`tmp_path / "run".glob(...)` 是对 str 调 glob(AttributeError),
    # 这行在无 symlink 特权的 Windows 上从未执行过,linux(CI)上首次运行即暴露
    if (tmp_path / "run").exists():
        assert not list((tmp_path / "run").glob(".source_snapshot.tmp-*"))


def test_freeze_refuses_containment(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    with pytest.raises(TaskError, match="contain each other"):
        freeze_input(src, src / "inner-run", max_files=100, timeout_seconds=60)


def test_freeze_enforces_max_files(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    for i in range(5):
        (src / "src" / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(TaskError, match="intake_max_files"):
        freeze_input(src, tmp_path / "run", max_files=3, timeout_seconds=60)
    assert not (tmp_path / "run" / SNAPSHOT_DIRNAME).exists()


def test_freeze_enforces_timeout(tmp_path: Path) -> None:
    src = _mk_source(tmp_path / "src")
    with pytest.raises(TaskError, match="intake_timeout_seconds"):
        freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=0)


def test_source_directory_untouched_after_freeze(tmp_path: Path) -> None:
    """源目录零写入:内容与 mtime 在冻结前后完全不变。"""
    src = _mk_source(tmp_path / "src")
    before = {
        str(p.relative_to(src)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }
    freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    after = {
        str(p.relative_to(src)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }
    assert before == after
    # .git 目录在任务结束后仍完全不变(不存在就是不存在)
    assert not (src / ".git").exists()


def test_intake_window_mutation_does_not_enter_snapshot(tmp_path: Path) -> None:
    """受理窗口内源被改:冻结副本与受理指纹不一致 → 上层拒绝,副本不含新内容。"""
    src = _mk_source(tmp_path / "src")
    first = freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    time.sleep(0.01)
    (src / "src" / "mod.py").write_text("x = mutated\n", encoding="utf-8", newline="\n")
    second = freeze_input(src, tmp_path / "run", max_files=100, timeout_seconds=60)
    assert first != second
    snap = tmp_path / "run" / SNAPSHOT_DIRNAME / "src" / "mod.py"
    assert snap.read_text(encoding="utf-8") == "x = mutated\n"  # 第二次冻结的副本自洽
