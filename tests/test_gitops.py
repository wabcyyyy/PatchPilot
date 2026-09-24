"""M1 gitops 基座测试:快照、diff、补丁应用、回滚与越权路径拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import TaskError
from app.gitops.cmd import run_git
from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch, check_patch
from app.gitops.rollback import reset_workspace, working_tree_is_clean
from app.gitops.snapshot import create_workspace


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _edit_source_file(workspace: Path, marker: str) -> None:
    f = workspace / "src" / "dateparse.py"
    # newline="\n":保持 LF,避免 Windows 默认行尾转换把整个文件改写
    f.write_text(_read(f) + f"\n# {marker}\n", encoding="utf-8", newline="\n")


def test_create_workspace_isolated(demo_repo: Path, tmp_path: Path) -> None:
    baseline = create_workspace(demo_repo, tmp_path / "ws")
    assert len(baseline) == 40

    # 工作区改动不影响源仓库
    _edit_source_file(tmp_path / "ws", "agent change")
    assert working_tree_is_clean(demo_repo)
    assert "agent change" not in _read(demo_repo / "src" / "dateparse.py")


def test_create_workspace_rejects_bad_source(tmp_path: Path) -> None:
    with pytest.raises(TaskError):
        create_workspace(tmp_path / "missing", tmp_path / "ws")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(TaskError):
        create_workspace(plain, tmp_path / "ws2")


def test_create_workspace_refuses_to_shadow_source(demo_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(TaskError):
        create_workspace(demo_repo, demo_repo / "nested")


def test_diff_detects_change_and_lists_files(demo_repo: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    create_workspace(demo_repo, ws)
    diff = working_tree_diff(ws)
    assert diff.is_empty and diff.changed_files == []

    _edit_source_file(ws, "probe")
    (ws / "new_file.txt").write_text("brand new\n", encoding="utf-8")
    diff = working_tree_diff(ws)
    assert not diff.is_empty
    assert set(diff.changed_files) == {"src/dateparse.py", "new_file.txt"}
    assert "@@" in diff.diff_text
    assert "new_file.txt" in diff.diff_text  # intent-to-add 让新文件出现在 diff 中


def test_apply_patch_roundtrip(demo_repo: Path, tmp_path: Path) -> None:
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    create_workspace(demo_repo, ws1)
    create_workspace(demo_repo, ws2)
    _edit_source_file(ws1, "patched")
    diff = working_tree_diff(ws1)

    ok, detail = check_patch(ws2, diff.diff_text)
    assert ok, detail
    result = apply_patch(ws2, diff.diff_text)
    assert result.applied and result.rejected_reason is None
    assert _read(ws1 / "src" / "dateparse.py") == _read(ws2 / "src" / "dateparse.py")


def test_apply_patch_rejects_garbage(demo_repo: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    create_workspace(demo_repo, ws)
    result = apply_patch(ws, "this is not a patch")
    assert not result.applied
    assert result.rejected_reason == "git apply --check failed"

    result = apply_patch(ws, "   ")
    assert not result.applied and result.rejected_reason == "empty patch"


def test_apply_patch_rejects_path_escape(demo_repo: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    baseline = create_workspace(demo_repo, ws)
    malicious = (
        "diff --git a/../outside.txt b/../outside.txt\n"
        "new file mode 100644\n"
        "index 0000000..7898192\n"
        "--- /dev/null\n"
        "+++ b/../outside.txt\n"
        "@@ -0,0 +1 @@\n"
        "+pwned\n"
    )
    result = apply_patch(ws, malicious)
    assert not result.applied
    # 越权补丁不得落地:工作区仍与基线一致
    reset_workspace(ws, baseline)
    assert working_tree_is_clean(ws)
    assert not (tmp_path / "outside.txt").exists()


# ---------- ATTACK-009(E1):apply 前落点校验 ----------


def test_apply_patch_rejects_escape_before_git_check(demo_repo: Path, tmp_path: Path) -> None:
    """②分支:`../` 落点 resolve 后越出工作树,在 git apply --check 之前
    即被结构化拒绝,reason 为 path_escape。"""
    ws = tmp_path / "ws"
    create_workspace(demo_repo, ws)
    malicious = (
        "diff --git a/../evil.txt b/../evil.txt\n"
        "new file mode 100644\n"
        "index 0000000..7898192\n"
        "--- /dev/null\n"
        "+++ b/../evil.txt\n"
        "@@ -0,0 +1 @@\n"
        "+pwned\n"
    )
    result = apply_patch(ws, malicious)
    assert not result.applied
    assert result.rejected_reason == "path_escape"
    assert "[path_escape]" in result.detail
    assert not (tmp_path / "evil.txt").exists()


def test_apply_patch_rejects_symlink_target(demo_repo: Path, tmp_path: Path, monkeypatch) -> None:
    """①分支:patch 目标路径本身是工作区内软链 → 拒 symlink_escape。

    软链指向工作区内合法文件——危害不在落点(②放行)而在"既有软链被
    补丁触碰后可被换向/利用",故结构上直接拒绝。
    """
    ws = tmp_path / "ws"
    create_workspace(demo_repo, ws)
    real = ws / "src" / "dateparse.py"
    link = ws / "src" / "linked.py"
    link.write_text(_read(real), encoding="utf-8", newline="\n")
    try:
        link.unlink()
        link.symlink_to(real)
        real_symlink = True
    except (OSError, NotImplementedError):
        # 本机(Windows 无 symlink 特权)无法物化真实软链:仅对该路径
        # 定向 mock is_symlink 探测驱动同一分支,不新增 skip。
        real_symlink = False
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: True if self == link else real_is_symlink(self),
        )
    patch = (
        "diff --git a/src/linked.py b/src/linked.py\n"
        "index 1234567..89abcde 100644\n"
        "--- a/src/linked.py\n"
        "+++ b/src/linked.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    result = apply_patch(ws, patch)
    assert not result.applied
    assert result.rejected_reason == "symlink_escape"
    assert "[symlink_escape]" in result.detail
    if real_symlink:
        assert link.is_symlink()  # 拒绝不落地:软链本身未被破坏


def test_rollback_restores_baseline(demo_repo: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    baseline = create_workspace(demo_repo, ws)
    original = _read(ws / "src" / "dateparse.py")

    _edit_source_file(ws, "dirty")
    (ws / "untracked.txt").write_text("junk\n", encoding="utf-8")
    run_git(ws, "rm", "--cached", "-q", "tests/test_dateparse.py")  # index 也要被还原
    assert not working_tree_is_clean(ws)

    reset_workspace(ws, baseline)
    assert working_tree_is_clean(ws)
    assert _read(ws / "src" / "dateparse.py") == original
    assert (ws / "tests" / "test_dateparse.py").exists()
    assert not (ws / "untracked.txt").exists()


# ---------- 审计整改(P0-3):生产入口的包含检查 ----------


def test_materialize_repo_refuses_nesting(tmp_path: Path) -> None:
    """模板与目标互为祖先时必须结构化报错,而非 copytree 递归展开。"""
    from app.errors import TaskError
    from app.gitops.testing import materialize_repo

    template = tmp_path / "tpl"
    template.mkdir()
    (template / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(TaskError, match="contain each other"):
        materialize_repo(template, template / "nested" / "ws")
    # 反向嵌套(dest 是 template 的祖先):dest 内放模板目录再以 dest 为目标
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "tpl").mkdir()
    (outer / "tpl" / "b.txt").write_text("x", encoding="utf-8")
    with pytest.raises(TaskError, match="contain each other"):
        materialize_repo(outer / "tpl", outer)
