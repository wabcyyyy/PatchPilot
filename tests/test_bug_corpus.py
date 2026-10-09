"""语料完整性:expected/reference.diff 必须与 repo/ 快照同步可 apply。

背景(2026-10-09 修复的存量缺陷):BUG-014 的 gold diff 对着格式化前的旧源码生成,
M10 修 replay diff 时漏掉了它——test_blockpatch 由此被迫只比"改了哪些文件"。
本文件把"gold diff ↔ repo 快照同步"钉成回归,防同族缺陷再次入库存而不自知。

安全实现注意:bugs/*/repo 位于主仓 worktree **内部**,在原位跑 `git apply` 会向上
找到主仓 .git,路径语义错且非 --check 模式会写进主仓——因此必须把被改文件复制到
仓库外(tmp_path 由 conftest 保证在仓库外)再校验。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BUGS_ROOT = Path(__file__).resolve().parent.parent / "bugs"


def _bug_dirs() -> list[Path]:
    return sorted(
        d
        for d in BUGS_ROOT.iterdir()
        if d.is_dir() and (d.name.startswith("BUG-") or d.name.startswith("SWE-"))
    )


def _touched_paths(diff_text: str) -> list[str]:
    """gold diff 触及的目标路径(+++ b/ 行);/dev/null = 新文件,无需复制。"""
    return [line[len("+++ b/") :] for line in diff_text.splitlines() if line.startswith("+++ b/")]


def test_every_bug_has_reference_diff() -> None:
    """有 repo 快照的题必须有 gold diff——metrics 的 expected_coverage 以它为分母来源。"""
    missing = [d.name for d in _bug_dirs() if not (d / "expected" / "reference.diff").exists()]
    assert missing == [], f"缺 expected/reference.diff: {missing}"


def test_reference_diff_applies_to_repo_snapshot(tmp_path: Path) -> None:
    """逐题把 diff 触及的文件复制到仓库外,git apply --check 只读校验。"""
    failures: list[str] = []
    for bug_dir in _bug_dirs():
        diff_file = bug_dir / "expected" / "reference.diff"
        diff_text = diff_file.read_text(encoding="utf-8")
        work = tmp_path / bug_dir.name
        work.mkdir()
        for rel in _touched_paths(diff_text):
            src = bug_dir / "repo" / rel
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.copyfile(src, dst)
        proc = subprocess.run(
            ["git", "apply", "--check", str(diff_file.resolve())],
            cwd=work,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            failures.append(f"{bug_dir.name}: {(proc.stderr or '').strip().splitlines()[0]}")
    assert failures == [], f"gold diff 与 repo 快照不同步: {failures}"
