"""M3.6 / M2.5 用例:检索遍历域与列表输出体量解耦,大仓库骨架出目录汇总。

缺陷本体(PROGRESS.md D.12):`list_files` 的 500 条上限原本是给"输出体量"设的,
但它返回的清单同时被 `search_code`/`find_symbol` 当作遍历域——排序第 500 个之后的文件
对模型彻底不可见。实测 9 道真实难题里 4 道的必改文件落在范围外
(sphinx-7590 的 `sphinx/util/cfamily.py` 下标 631、scikit-learn-12682 的两处 502/903)。
这组用例把"必改文件在 500 名之后也搜得到"与"没搜全时必须如实标注"钉住。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.context.repo_map import build_repo_map
from app.tools.base import ToolContext
from app.tools.files import list_files, search_code
from app.tools.symbols import find_symbol
from app.tools.tracker import Tracker

HAS_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAS_RG, reason="ripgrep 不在这台机器上")

NEEDLE_FILE = "pkg/f599.py"  # 排序后第 600 个:正好落在旧 500 条上限之外
NEEDLE = "suspicious_marker_only_at_the_tail"


def _ctx(workspace: Path, **overrides: Any) -> ToolContext:
    return ToolContext(
        task_id="T-SCOPE",
        workspace=workspace,
        baseline_commit="",
        tracker=Tracker(None, task_id="T-SCOPE"),
        report_dir=workspace.parent,
        test_sets={},
        allowed_paths=None,
        **overrides,
    )


def _override(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    settings = get_settings()
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture()
def big_repo(tmp_path: Path) -> Path:
    """600 个 `pkg/fNNN.py`,只有最后一个含目标串;`pkg/z_last/` 整目录也在 500 名之外。"""
    root = tmp_path / "repo"
    for index in range(600):
        path = root / "pkg" / f"f{index:03d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = NEEDLE if index == 599 else f"VALUE = {index}"
        path.write_text(f"{body}\n", encoding="utf-8")
    deep = root / "pkg" / "z_last" / "deep"
    deep.mkdir(parents=True)
    (deep / "hidden_target.py").write_text(
        "def tail_only_symbol():\n    return 1\n", encoding="utf-8"
    )
    return root


def test_search_reaches_files_beyond_the_listing_cap(big_repo: Path) -> None:
    """核心回归钉子:第 600 个文件里的命中必须搜得到(旧实现在这里给空结果)。"""
    result = search_code(_ctx(big_repo), NEEDLE)

    assert result.ok
    assert [item["path"] for item in result.output["matches"]] == [NEEDLE_FILE]
    assert result.output["scope_truncated"] is False  # 8000 的默认遍历域覆盖得下


def test_list_files_still_caps_output_and_says_so(big_repo: Path) -> None:
    """列表档不变(500 条),但 `truncated` 必须如实标注——旧实现只给 count 不告知裁过。"""
    result = list_files(_ctx(big_repo))

    assert result.ok
    assert result.output["count"] == 500
    assert result.output["truncated"] is True


def test_small_repo_listing_is_not_marked_truncated(tmp_path: Path) -> None:
    root = tmp_path / "small"
    root.mkdir()
    for name in ("a.py", "b.py", "c.txt"):
        (root / name).write_text("x\n", encoding="utf-8")

    result = list_files(_ctx(root))

    assert result.output["count"] == 3
    assert result.output["truncated"] is False


def test_scope_cap_is_honoured_and_reported(
    big_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把遍历域压到 100:尾部的命中拿不到,但工具必须说"这次没搜全",不给假称穷尽的空结论。"""
    _override(monkeypatch, max_search_files=100)

    result = search_code(_ctx(big_repo), NEEDLE)

    assert result.ok
    assert result.output["matches"] == []
    assert result.output["scope_truncated"] is True


def test_find_symbol_is_decoupled_from_the_listing_cap(big_repo: Path) -> None:
    """第 600 个文件里定义的符号也要能跳转(它同样在旧清单上限之外)。"""
    result = find_symbol(_ctx(big_repo), "tail_only_symbol")

    assert result.ok
    assert result.output["match"] == "exact"
    assert result.output["matches"][0]["path"] == "pkg/z_last/deep/hidden_target.py"
    assert result.output["scope_truncated"] is False


@needs_rg
def test_engines_stay_identical_on_a_large_tree(
    big_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解耦之后两臂/两引擎仍必须同输出:600 文件的树上逐条比对。"""
    rg_result = search_code(_ctx(big_repo), NEEDLE)
    _override(monkeypatch, search_engine="python")
    py_result = search_code(_ctx(big_repo), NEEDLE)

    assert rg_result.output["matches"] == py_result.output["matches"]
    assert rg_result.output["engine"] == "rg"
    assert py_result.output["engine"] == "python"


def test_search_scope_settings_defaults_and_validators() -> None:
    defaults = Settings()
    assert defaults.max_search_files == 8000
    assert defaults.repo_map_dir_depth == 3
    with pytest.raises(ValueError, match="max_search_files"):
        Settings(max_search_files=0)
    with pytest.raises(ValueError, match="repo_map_dir_depth"):
        Settings(repo_map_dir_depth=0)


# ---------- M2.5:大仓库的目录级汇总 ----------


@pytest.fixture()
def wide_repo(tmp_path: Path) -> Path:
    """两个顶层目录,合计 12 个文件;`zzz/` 整目录在 max_files=10 的取样集之外。"""
    root = tmp_path / "wide"
    for index in range(9):
        path = root / "aaa" / f"m{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def a{index}():\n    return {index}\n", encoding="utf-8")
    for name in ("late_one.py", "late_two.py"):
        deep = root / "zzz" / "sub"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / name).write_text(f"def {name[:-3]}():\n    return 0\n", encoding="utf-8")
    return root


def test_large_repo_renders_directory_rollup(wide_repo: Path) -> None:
    """文件数超过取样上限时,结构档必须是目录汇总:被裁掉的 `zzz/sub/` 也要在场。"""
    rendered = build_repo_map(wide_repo, max_chars=4000, max_files=10, dir_depth=3)

    assert "aaa/  (9 files, 9 py)" in rendered
    assert "zzz/sub/  (2 files, 2 py)" in rendered  # 旧实现这里整目录凭空消失
    assert "per-file outlines shown for a 10-file sample" in rendered


def test_small_repo_skeleton_shape_is_unchanged(tmp_path: Path) -> None:
    """小仓库逐字保持 M2 形状:列文件、不列目录汇总、不带大仓库说明行。"""
    root = tmp_path / "tiny"
    root.mkdir()
    (root / "only.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    rendered = build_repo_map(root, max_chars=4000, max_files=200)

    assert "only.py [lines 2]" in rendered
    assert "files, " not in rendered  # 没有目录汇总行
    assert "per-file outlines" not in rendered


def test_dir_rollup_merges_deeper_paths_into_the_depth_cut(wide_repo: Path) -> None:
    """深度切点是刻意的:第 3 层以下的目录并进祖先,行数由目录数决定而不是文件数决定。"""
    rendered = build_repo_map(wide_repo, max_chars=4000, max_files=10, dir_depth=1)

    assert "aaa/  (9 files, 9 py)" in rendered
    assert "zzz/  (2 files, 2 py)" in rendered  # zzz/sub 并进顶层祖先
