"""块协议解析器/编译器测试:文法、锚定唯一性、行尾保持、门禁穿透、语料 round-trip。

unified diff 仍是内部唯一事实源:本文件既要证明块文法本身成立,也要证明
编译产物在既有防线链(门禁 → git apply --check → 应用)下与原始 diff 等价。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.gitops.blockpatch import (
    BlockPatchError,
    compile_block_patch,
    parse_block_patch,
    split_keepends,
    unified_to_block,
)
from app.gitops.patcher import apply_patch as git_apply_patch
from app.gitops.testing import materialize_repo
from app.graph.gates import parse_diff_files, run_gates

BUGS_ROOT = Path("bugs")


def _block(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch", ""))


def _write(ws: Path, rel: str, text: str, newline: str = "\n") -> Path:
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline=newline)
    return target


# ---------------- 解析 ----------------


def test_parse_three_section_types() -> None:
    sections = parse_block_patch(
        _block(
            "*** Update File: src/a.py",
            "@@ def f():",
            "     return 1",
            "+    return 2",
            "*** Add File: src/b.py",
            "+X = 1",
            "*** Delete File: src/c.py",
        )
    )
    assert [s.action for s in sections] == ["update", "add", "delete"]
    assert [s.path for s in sections] == ["src/a.py", "src/b.py", "src/c.py"]
    # @@ 提示行整行保留在 body 里,由编译期当分块边界
    assert sections[0].lines == ["@@ def f():", "     return 1", "+    return 2"]
    assert sections[2].lines == []


def test_parse_normalizes_section_path() -> None:
    sections = parse_block_patch(_block("*** Add File: .\\src\\util.py\\", "+ok"))
    assert sections[0].path == "src/util.py"


def test_blank_context_line_and_backslash_escape() -> None:
    sections = parse_block_patch(_block("*** Update File: src/a.py", " ", "\\*** Begin Patch"))
    assert sections[0].lines == [" ", "\\*** Begin Patch"]


def test_markdown_fence_is_tolerated() -> None:
    text = "```diff\n" + _block("*** Add File: src/a.py", "+ok") + "```\n"
    assert parse_block_patch(text)[0].path == "src/a.py"


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "empty_patch"),
        ("   ", "empty_patch"),
        ("*** Add File: a.py\n+ok\n*** End Patch\n", "bad_header"),
        ("*** Begin Patch\n*** Add File: a.py\n+ok\n", "unbalanced"),
        ("*** Begin Patch\n+ok\n*** End Patch\n", "bad_header"),
        ("*** Begin Patch\n*** Rename File: a.py\n*** End Patch\n", "bad_header"),
        ("*** Begin Patch\n*** Add File: \n*** End Patch\n", "missing_path"),
        (
            _block("*** Add File: src/a.py", "+ok", "-bad"),
            "bad_header",
        ),
        (
            _block("*** Update File: src/a.py", "x = 1"),
            "bad_header",
        ),
        (
            _block("*** Delete File: src/a.py", "-x"),
            "delete_body_not_empty",
        ),
        (
            _block("*** Update File: src/a.py", " ok", "*** Add File: src/a.py", "+ok"),
            "duplicate_path",
        ),
    ],
)
def test_parse_rejects_bad_grammar(text: str, reason: str) -> None:
    with pytest.raises(BlockPatchError) as exc:
        parse_block_patch(text)
    assert exc.value.reason == reason
    assert exc.value.tag in {"format"}


@pytest.mark.parametrize(
    "path",
    ["../escape.py", "/abs/path.py", ".git/config", "src/../../x.py", ".git/x.py", "C:/x/a.py"],
)
def test_parse_rejects_hostile_paths_before_any_file_read(path: str) -> None:
    """段头预检走门禁词表的 paths:编译期会读目标文件,越界必须更早拒。"""
    with pytest.raises(BlockPatchError) as exc:
        parse_block_patch(_block(f"*** Update File: {path}", " ok"))
    assert (exc.value.tag, exc.value.reason) == ("paths", "path_escape")


def test_raw_unified_diff_is_rejected_proving_single_entry() -> None:
    """块协议是唯一入口:模型直喂 unified diff 只能是协议错误,不存在格式猜测。"""
    unified = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(BlockPatchError) as exc:
        parse_block_patch(unified)
    assert exc.value.reason == "bad_header"


# ---------------- 编译 ----------------


def test_compile_update_uses_real_line_numbers_and_keeps_the_rest(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    diff = compile_block_patch(
        tmp_path,
        parse_block_patch(
            _block("*** Update File: src/a.py", " def f():", "-    return 1", "+    return 3")
        ),
    )
    assert diff.startswith(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,5 +1,5 @@"
    )
    assert "-    return 1\n+    return 3\n" in diff


def test_compile_anchor_must_be_unique(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "x\ny\nx\ny\n")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path, parse_block_patch(_block("*** Update File: src/a.py", " x", "-y", "+z"))
        )
    assert exc.value.reason == "ambiguous_anchor"
    assert "出现 2 处" in exc.value.detail and "更多上下文行" in exc.value.detail

    diff = compile_block_patch(
        tmp_path,
        parse_block_patch(_block("*** Update File: src/a.py", " x", "-y", " x", "-y", "+z", "+z")),
    )
    assert diff.count("@@ ") == 1 and "+z" in diff


def test_compile_anchor_not_found_names_the_file_and_first_line(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "x\n")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path, parse_block_patch(_block("*** Update File: src/a.py", "-nope", "+y"))
        )
    assert exc.value.reason == "anchor_not_found"
    assert "逐字一致" in exc.value.detail


def test_compile_multi_chunk_section_produces_two_hunks(tmp_path: Path) -> None:
    body = "\n".join([f"line{i}" for i in range(30)]) + "\n"
    _write(tmp_path, "src/a.py", body)
    diff = compile_block_patch(
        tmp_path,
        parse_block_patch(
            _block(
                "*** Update File: src/a.py",
                " line1",
                "-line2",
                "+CHANGED_A",
                "@@",
                " line25",
                "-line26",
                "+CHANGED_B",
            )
        ),
    )
    assert diff.count("@@ ") == 2
    assert "+CHANGED_A" in diff and "+CHANGED_B" in diff


def test_compile_rejects_out_of_order_chunks(tmp_path: Path) -> None:
    body = "\n".join([f"line{i}" for i in range(30)]) + "\n"
    _write(tmp_path, "src/a.py", body)
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path,
            parse_block_patch(
                _block(
                    "*** Update File: src/a.py",
                    " line25",
                    "-line26",
                    "+B",
                    "@@",
                    " line1",
                    "-line2",
                    "+A",
                )
            ),
        )
    assert exc.value.reason == "chunks_out_of_order"


def test_compile_context_only_section_is_not_a_change(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "x\ny\n")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(tmp_path, parse_block_patch(_block("*** Update File: src/a.py", " x")))
    assert exc.value.reason == "empty_section"


def test_compile_add_and_delete_target_states(tmp_path: Path) -> None:
    _write(tmp_path, "src/exists.py", "x\n")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path, parse_block_patch(_block("*** Add File: src/exists.py", "+y"))
        )
    assert exc.value.reason == "add_exists"
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(tmp_path, parse_block_patch(_block("*** Delete File: src/gone.py")))
    assert exc.value.reason == "delete_missing"
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path, parse_block_patch(_block("*** Update File: src/gone.py", "-x"))
        )
    assert exc.value.reason == "target_missing"


def test_compile_add_always_emits_regular_file_mode(tmp_path: Path) -> None:
    """软链门禁的前提:块协议结构上产不出 120000,新增只能是 100644。"""
    diff = compile_block_patch(
        tmp_path, parse_block_patch(_block("*** Add File: src/n.py", "+../outside/evil.py"))
    )
    assert "new file mode 100644" in diff
    assert "120000" not in diff and "rename" not in diff and "copy to" not in diff


def test_compile_crlf_and_missing_trailing_newline(tmp_path: Path) -> None:
    crlf = _write(tmp_path, "src/c.py", "def f():\r\n    return 1\r\n")
    diff = compile_block_patch(
        tmp_path,
        parse_block_patch(
            _block("*** Update File: src/c.py", " def f():", "-    return 1", "+    return 2")
        ),
    )
    assert "-    return 1\r\n" in diff and "+    return 2\r\n" in diff
    assert crlf.read_bytes().count(b"\r\n") == 2

    _write(tmp_path, "src/n.py", "a\nb")  # 末行无行尾符
    diff = compile_block_patch(
        tmp_path, parse_block_patch(_block("*** Update File: src/n.py", " a", "-b", "+c"))
    )
    assert "\\\n" not in diff  # 标记行独立成行
    assert "\\ No newline at end of file\n" in diff


def test_split_keepends_only_splits_on_lf() -> None:
    assert split_keepends("a\u2028b\nc") == ["a\u2028b\n", "c"]
    assert split_keepends("") == []


def test_compile_rejects_binary_and_non_utf8_targets(tmp_path: Path) -> None:
    _write(tmp_path, "src/b.py", "x\x00y\n")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(tmp_path, parse_block_patch(_block("*** Update File: src/b.py", "-xy")))
    assert exc.value.reason == "binary_target"
    (tmp_path / "src" / "g.py").write_bytes("x\n".encode("gbk") + b"\xff\xfe")
    with pytest.raises(BlockPatchError) as exc:
        compile_block_patch(
            tmp_path, parse_block_patch(_block("*** Update File: src/g.py", "-x", "+y"))
        )
    assert exc.value.reason == "encoding_unsupported"


# ---------------- 门禁穿透:编译产物仍走同一条防线链 ----------------


def _gates(diff: str):
    return run_gates(diff, allowed_paths=["src/**"], max_files=5)


def test_block_add_of_shadow_module_still_blocked_by_gates() -> None:
    """块协议换不了判定:Add `pytest.py` 编译后仍被影子门禁拒。"""
    compiled = compile_block_patch(
        Path("."), parse_block_patch(_block("*** Add File: pytest.py", "+def main(): pass"))
    )
    gate = _gates(compiled)
    assert not gate.ok and any(v.gate == "shadow" for v in gate.violations), gate.violations


def test_block_touching_test_file_still_blocked_by_gates() -> None:
    compiled = compile_block_patch(
        Path("."), parse_block_patch(_block("*** Add File: tests/test_x.py", "+def t(): pass"))
    )
    gate = _gates(compiled)
    assert not gate.ok and any(v.gate == "files" for v in gate.violations)


def test_block_out_of_allowed_paths_still_blocked_by_gates(tmp_path: Path) -> None:
    _write(tmp_path, "docs/note.md", "x\n")
    compiled = compile_block_patch(
        tmp_path, parse_block_patch(_block("*** Update File: docs/note.md", "-x", "+y"))
    )
    gate = _gates(compiled)
    assert not gate.ok and any(v.gate == "paths" for v in gate.violations)


def test_unified_to_block_maps_symlink_diff_to_plain_add(tmp_path: Path) -> None:
    """软链攻击在协议层结构上不可表示:120000 段只能映射成普通 Add。"""
    block = unified_to_block(
        "diff --git a/src/labels.py b/src/labels.py\n"
        "new file mode 120000\n"
        "index 0000000..31e2f45\n"
        "--- /dev/null\n"
        "+++ b/src/labels.py\n"
        "@@ -0,0 +1 @@\n"
        "+../outside/evil_mod.py\n"
    )
    assert "*** Add File: src/labels.py" in block
    compiled = compile_block_patch(tmp_path, parse_block_patch(block))
    assert "120000" not in compiled
    assert _gates(compiled).ok  # 形态已退化为合法新增,真正的防线是协议层产不出软链


# ---------------- 语料 round-trip:块 → compile → git apply ≡ 原 diff ----------------


def _corpus() -> list[tuple[str, str, str]]:
    """现存回放语料里每一个 apply_patch 补丁(去重),返回 (bug 目录相对名, 标签, 块文本)。

    迁移前这里跑的是"原 unified ↔ 块编译产物"双应用等价(卡1 已实测 36/36 通过,
    即机械迁移未改语义的证据);迁移后语料只剩块文本,而 `expected/reference.diff`
    不能当基准——BUG-014 的 reference.diff 存量就已与 repo 源码不符(删除行写成
    `items[i:i + n + 1]`,实际是 `items[i : i + n + 1]`),拿它对比会把存量数据缺陷
    伪装成协议缺陷。故本表钉的是回放补丁在新协议下的持久不变量:能解析、能编译、
    能通过 `git apply --check` 并真实应用,且改动文件集合与题面 gold 范围一致。
    """
    cases: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(BUGS_ROOT.glob("**/replay/*.json")):
        bug_dir = path.parent.parent
        bug_key = bug_dir.relative_to(BUGS_ROOT).as_posix()
        data = json.loads(path.read_text(encoding="utf-8"))
        steps = data if isinstance(data, list) else data.get("steps", [])
        for step in steps:
            if step.get("tool") != "apply_patch":
                continue
            args = step.get("args", {})
            if "patch_text" in args:
                block_text = str(args["patch_text"])
            elif "diff_text" in args:
                block_text = unified_to_block(str(args["diff_text"]))
            else:
                continue
            if not block_text.strip() or (bug_key, block_text) in seen:
                continue
            seen.add((bug_key, block_text))
            cases.append((bug_key, f"{bug_key}/{path.stem}", block_text))
    assert cases, "回放语料为空:round-trip 表失去意义"
    return cases


@pytest.mark.parametrize(
    "bug_key,label,block_text",
    _corpus(),
    ids=[b.replace("/", "-") for b, _, _ in _corpus()],
)
def test_corpus_replays_still_apply_through_block_protocol(
    bug_key: str, label: str, block_text: str, baseline_repo, tmp_path: Path
) -> None:
    sections = parse_block_patch(block_text)
    ws = shutil.copytree(baseline_repo(bug_key), tmp_path / "ws")
    compiled = compile_block_patch(ws, sections)

    assert "120000" not in compiled and "rename to" not in compiled and "copy to" not in compiled
    applied = git_apply_patch(ws, compiled)
    assert applied.applied, f"{label} 回放补丁被拒:{applied.rejected_reason} {applied.detail}"

    touched = _touched_files(ws)
    reference = BUGS_ROOT / bug_key / "expected" / "reference.diff"
    if reference.exists():
        # 只比"改了哪些文件":reference.diff 的 hunk 内容可能过期(BUG-014 即如此),
        # 但文件集合是 metrics 的定位口径,回放补丁改动它就该与之一致。
        expected = sorted(set(parse_diff_files(reference.read_text(encoding="utf-8"))))
        assert touched == expected, f"{label} 改动范围与题面 gold 不一致:{touched} != {expected}"
    assert _tree_map(ws) != _tree_map(baseline_repo(bug_key)), f"{label} 补丁没有改变任何文件"


def _touched_files(ws: Path) -> list[str]:
    from app.gitops.differ import working_tree_diff

    return sorted(working_tree_diff(ws).changed_files)


def _repo_with(tmp: Path, files: dict[str, str]) -> Path:
    """物化一个行尾确定的小仓库。

    本机 git 默认 `core.autocrlf=true`,checkout 会把 LF 变 CRLF,手写 LF 补丁就
    "patch does not apply"——那是环境而不是协议问题。模板里放 `.gitattributes`
    (`* text eol=lf`)把行尾钉成 LF,两侧副本才可比。
    """
    tpl = tmp / "tpl"
    _write(tpl, ".gitattributes", "* text eol=lf\n")
    for rel, body in files.items():
        _write(tpl, rel, body)
    ws = tmp / "ws"
    materialize_repo(tpl, ws, extra_commit=False)
    return ws


@pytest.mark.parametrize(
    "diff_text",
    [
        # 单 hunk 修改
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,3 @@\n x\n+z\n y\n",
        # 同文件双 hunk(未变动区足够宽,编译后仍是两个 hunk)。
        # hunk 必须带上下文行:git apply 默认拒绝零上下文补丁(全链没用 --unidiff-zero),
        # 而编译器恒发 n=3 上下文,所以真实路径不受影响。
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n 1\n-2\n+B2\n 3\n"
        "@@ -27,3 +27,3 @@\n 27\n-28\n+B28\n 29\n",
        # 新增文件
        "diff --git a/src/n.py b/src/n.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/src/n.py\n@@ -0,0 +1,2 @@\n+p = 1\n+q = 2\n",
        # 删除整文件
        "diff --git a/src/d.py b/src/d.py\ndeleted file mode 100644\n--- a/src/d.py\n"
        "+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n",
        # 多文件混合(改一个 + 新增一个);git apply 认多段要靠 diff --git 头
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n-x\n+z\n y\n"
        "diff --git a/src/b.py b/src/b.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/src/b.py\n@@ -0,0 +1 @@\n+made\n",
    ],
)
def test_compiled_block_diff_is_equivalent_to_original_diff(diff_text: str, tmp_path: Path) -> None:
    """双向等价:原 diff 与"块→编译"产物应用后,工作树逐字节相同。

    这是块协议的核心正确性承诺——协议只是外语法,不改补丁语义。语料迁移前
    同一性质覆盖过全部 36 个回放 diff(卡1 实测),这里保留可手写的最小形态。
    """
    if "deleted file mode" in diff_text:
        files = {"src/d.py": "x\ny\n"}
    elif "@@ -1,3 +1,3 @@" in diff_text:
        files = {"src/a.py": "".join(f"{i}\n" for i in range(1, 31))}
    else:
        files = {"src/a.py": "x\ny\n"}
    original_ws = _repo_with(tmp_path / "a", files)
    applied_original = git_apply_patch(original_ws, diff_text)
    assert applied_original.applied, applied_original.detail

    compiled_ws = _repo_with(tmp_path / "b", files)
    compiled = compile_block_patch(compiled_ws, parse_block_patch(unified_to_block(diff_text)))
    applied_compiled = git_apply_patch(compiled_ws, compiled)
    assert applied_compiled.applied, f"{applied_compiled.rejected_reason} {applied_compiled.detail}"

    assert _tree_map(compiled_ws) == _tree_map(original_ws)


@pytest.fixture(scope="session")
def baseline_repo(
    tmp_path_factory: pytest.TempPathFactory,
):
    """每个 bug 只物化一次纯工作树(含 .git),用例各自 copytree 出隔离副本。"""
    cache: dict[str, Path] = {}

    def get(bug_key: str) -> Path:
        if bug_key not in cache:
            dest = tmp_path_factory.mktemp(f"base-{bug_key.replace('/', '-')}") / "repo"
            materialize_repo(BUGS_ROOT / bug_key / "repo", dest, extra_commit=False)
            cache[bug_key] = dest
        return cache[bug_key]

    return get


def _tree_map(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out
