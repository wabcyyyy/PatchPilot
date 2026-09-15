"""测试辅助:把静态模板目录物化为带 git 历史的临时仓库。

fixtures 目录(如 tests/fixtures/demo_repo)只保存工作树文件;
git 历史(≥2 个 commit)由本函数在临时目录现场构建,
避免主仓库内嵌套 .git。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.gitops.cmd import run_git

_TEMPLATE_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def materialize_repo(template_dir: Path | str, dest: Path | str, extra_commit: bool = True) -> str:
    """把模板目录变成 git 仓库(2 个 commit),返回 HEAD sha。

    - 局部关闭 autocrlf:补丁与 diff 依赖字节级行尾一致;
    - 第二个 commit 只改 README,保持被测源码行号稳定。
    """
    template = Path(template_dir)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dest, ignore=_TEMPLATE_IGNORE)

    run_git(dest, "init", "-q")
    run_git(dest, "config", "user.email", "fixture@patchpilot.local")
    run_git(dest, "config", "user.name", "PatchPilot Fixture")
    run_git(dest, "config", "core.autocrlf", "false")
    run_git(dest, "add", "-A")
    run_git(dest, "commit", "-q", "-m", "init: baseline")

    if extra_commit:
        readme = dest / "README.md"
        text = readme.read_text(encoding="utf-8") if readme.exists() else "# fixture\n"
        readme.write_text(
            text.rstrip("\n") + "\n\ncommit 2: docs only, source untouched.\n", encoding="utf-8"
        )
        run_git(dest, "add", "-A")
        run_git(dest, "commit", "-q", "-m", "docs: touch README")

    _, out, _ = run_git(dest, "rev-parse", "HEAD")
    return out.strip()
