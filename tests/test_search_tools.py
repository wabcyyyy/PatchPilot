"""M3 检索升级用例:引擎等价性(核心)+ 新参数形状 + 两个符号工具 + 边界守卫。

红线是**默认行为逐字等价**:`search_code` 不带新参数时的 `{path, line, text}` 条目、
"文件序 + 行序"排序、200 字符裁剪、`max_search_results` 取前 N 与 `truncated` 判定,
必须与旧实现完全一致——存量用例(`tests/test_tools.py`)与两臂消融对照都钉在这上面。

`_cap_matches` 复刻的是旧循环"达上限后在下一个文件的循环头 break"这一事实,
所以"刚好 N 条且其后还有文件"也报 `truncated=True`。用例把这个反直觉口径钉死:
将来谁要"修正"它,必须先改这里并留下理由。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.graph.nodes import READ_TOOLS, WRITE_TOOLS
from app.tools.base import ToolContext
from app.tools.files import _iter_repo_files, read_file, search_code
from app.tools.search import grep_files, resolve_engine
from app.tools.symbols import describe_file, find_symbol
from app.tools.tracker import Tracker

HAS_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAS_RG, reason="ripgrep 不在这台机器上")

# ---------- fixture 仓库:每条陷阱一个文件 ----------

# .gitignore 让 rg 的默认忽略规则真的会裁掉 ignored.py —— 我们的路径必须不裁(旧 rglob 口径)
GITIGNORE = "ignored.py\n"
IGNORED_TEXT = "parse_date gitignored-but-still-visible\n"
HIDDEN_TEXT = "parse_date inside-a-dotdir\n"
BINARY = b"\x00\x01parse_date in a binary file\n"
APP_TEXT = "import os\n\n\ndef parse_date(value):\n    return value\n"
UTIL_TEXT = (
    "def parse_datetime(value):\n    return value\n\n\ndef parse_date_range(a, b):\n    return a\n"
)
VENV_TEXT = "def parse_date_in_venv():\n    return 0\n"
NODE_TEXT = "parse_date in node_modules\n"
DASH_TEXT = "call it with --flag and --dash\n"
META_TEXT = 'count = sum(value for value in items)  # [meta]\nif x == y and z != "q":\n    pass\n'
LONG_TEXT = "parse_date " + "y" * 300 + "\n"
INDENT_TEXT = "def wrap():\n        parse_date   deeply_indented\n"
NUL_MID_TEXT = "x" * 1500 + "\n\x00\nparse_date past a mid-file nul\n"
DESCRIBE_SRC = '''"""模块 docstring。"""


@decorator
def top(value, other=3) -> int:
    return value


class Outer:
    class Inner:
        def deep(self):
            pass

    def method(self, a, *, b=1, **kwargs):
        def local():
            pass

        return a

    async def afetch(self, url: str) -> dict[str, int]:
        return {}


async def fetch(url: str, *, timeout=5.0) -> dict[str, int]:
    return {}
'''


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, ".gitignore", GITIGNORE)
    _write(root, "ignored.py", IGNORED_TEXT)
    _write(root, ".dotdir/hidden.py", HIDDEN_TEXT)
    _write(root, "src/app.py", APP_TEXT)
    _write(root, "src/util.py", UTIL_TEXT)
    _write(root, "src/dash.py", DASH_TEXT)
    _write(root, "src/meta.py", META_TEXT)
    _write(root, "src/long.py", LONG_TEXT)
    _write(root, "src/indent.py", INDENT_TEXT)
    _write(root, "src/nulmid.py", NUL_MID_TEXT)
    _write(root, ".venv/lib/hidden.py", VENV_TEXT)
    _write(root, "node_modules/pkg/index.js", NODE_TEXT)
    (root / "blob.bin").write_bytes(BINARY)
    return root


def _ctx(workspace: Path, **overrides: Any) -> ToolContext:
    return ToolContext(
        task_id="T-M3",
        workspace=workspace,
        baseline_commit="",
        tracker=Tracker(None, task_id="T-M3"),
        report_dir=workspace.parent,
        test_sets={},
        allowed_paths=None,
        **overrides,
    )


def _override(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    """改 Settings 的缓存实例(工具层每次调用现读),monkeypatch 负责还原。"""
    settings = get_settings()
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def _both_engines(
    root: Path, needle: str, *, glob: str | None = None, **kwargs: Any
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]], bool]:
    """同一份文件序列分别跑 Python 与 rg:返回 (py_matches, py_trunc, rg_matches, rg_trunc)。"""
    files = _iter_repo_files(root, glob)
    max_results: int = kwargs.pop("max_results", 50)
    py_matches, py_truncated = grep_files(
        root, needle, files=files, max_results=max_results, engine="python", **kwargs
    )
    rg_matches, rg_truncated = grep_files(
        root, needle, files=files, max_results=max_results, engine="rg", **kwargs
    )
    return py_matches, py_truncated, rg_matches, rg_truncated


# ---------- 核心:两条引擎的输出等价性 ----------

PARITY_QUERIES = [
    "parse_date",
    "PARSE_DATE",  # 大小写不敏感
    "-date",  # 前导减号:必须走 --expression,不能被 rg 当成选项
    "--flag",
    "def parse_date(",  # 字面模式下的正则元字符
    '== "q"',
    "past a mid-file nul",  # 1KB 之后有 NUL:--text 抹平与 Python 的差异
    "manytokens",
    "no-such-needle-anywhere",
]


@pytest.mark.parametrize("needle", PARITY_QUERIES)
@needs_rg
def test_rg_and_python_engines_are_identical(repo: Path, needle: str) -> None:
    """逐条查询核两引擎:同 matches(含顺序)、同 truncated。"""
    _write(repo, "src/many.py", "".join(f"manytokens line {i}\n" for i in range(60)))
    py_matches, py_truncated, rg_matches, rg_truncated = _both_engines(repo, needle)
    assert rg_matches == py_matches, f"两条引擎对 {needle!r} 的匹配不等价"
    assert rg_truncated == py_truncated, f"两条引擎对 {needle!r} 的截断判定不一致"


@needs_rg
@pytest.mark.parametrize(
    ("needle", "kwargs"),
    [
        ("parse_date", {"glob": "*.py"}),
        ("parse_date", {"glob": "src/*"}),
        ("parse_date", {"glob": "*.js"}),
        ("parse_date", {"context_lines": 1}),
        ("parse_date", {"context_lines": 3}),
        ("parse_date", {"per_file_cap": 1}),
        ("parse_date", {"per_file_cap": 2, "context_lines": 2}),
        ("parse_date", {"max_results": 5}),
        ("parse_date", {"max_results": 1}),
        ("parse_date", {"max_results": 0}),
        (r"parse_\w+", {"regex": True}),
        (r"parse_\w+", {"regex": True, "context_lines": 1}),
        (r"^\s+parse_date\s+", {"regex": True}),
    ],
)
def test_engines_identical_across_parameter_shapes(repo: Path, needle: str, kwargs: dict) -> None:
    _write(repo, "src/many.py", "".join(f"manytokens line {i}\n" for i in range(60)))
    py_matches, py_truncated, rg_matches, rg_truncated = _both_engines(repo, needle, **kwargs)
    assert rg_matches == py_matches, (needle, kwargs)
    assert rg_truncated == py_truncated, (needle, kwargs)


@needs_rg
def test_engines_identical_on_case_variant_filenames(repo: Path) -> None:
    """Windows 的 `sorted(Path)` 折叠大小写、POSIX 不折叠:两条路径必须给出同一个顺序。"""
    _write(repo, "src/Config.py", "parse_date in Config\n")
    _write(repo, "src/config2.py", "parse_date in config2\n")
    py_matches, py_truncated, rg_matches, rg_truncated = _both_engines(repo, "parse_date")
    assert rg_matches == py_matches and rg_truncated == py_truncated
    assert py_matches, "大小写变体文件确实进了结果"


@needs_rg
def test_engine_reports_rgrun_and_records_fallback(repo: Path) -> None:
    """加速生效就如实标注;跑不成(含 rc=2)就回落并把原因写进 meta,结果一字不变。"""
    meta: dict[str, Any] = {}
    files = _iter_repo_files(repo, None)
    grep_files(repo, "parse_date", files=files, engine="auto", meta=meta)
    assert meta["engine"] == "rg" and "fallback" not in meta, "真二进制跑不通就等于没有加速"

    # regex=True 一律由 Python 定案:Rust 正则方言 ≠ Python re,不给模型第二套语义
    needle = r"(?<=def )parse_\w+"
    py = grep_files(repo, needle, files=files, engine="python", regex=True)
    meta = {}
    rg = grep_files(repo, needle, files=files, engine="rg", regex=True, meta=meta)
    assert rg == py
    assert meta["engine"] == "python" and "regex" in meta["fallback"]


@needs_rg
def test_rg_failure_falls_back_without_changing_results(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rg 侧任何非"零匹配"的失败都只让路径变慢:退出码 2 → 回落 Python,输出与基准一致。"""
    import subprocess

    def _fake(cmd: Any, workspace: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="rg: boom")

    monkeypatch.setattr("app.tools.search._run_rg", _fake)
    files = _iter_repo_files(repo, None)
    meta: dict[str, Any] = {}
    rg = grep_files(repo, "parse_date", files=files, engine="rg", meta=meta)
    assert meta["engine"] == "python" and "exit 2" in meta["fallback"]
    assert rg == grep_files(repo, "parse_date", files=files, engine="python")


@needs_rg
def test_prefilter_disagreement_triggers_a_full_rescan(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rg 报了文件而 Python 一行没命中 = 两侧口径在打架,不许拿"空手"当结论:整仓复扫。"""
    monkeypatch.setattr("app.tools.search._rg_files", lambda *a, **k: ["src/util.py"])
    meta: dict[str, Any] = {}
    files = _iter_repo_files(repo, None)
    result = grep_files(repo, "def parse_date(", files=files, engine="rg", meta=meta)
    assert meta["engine"] == "python" and "rescan" in meta["fallback"]
    assert result == grep_files(repo, "def parse_date(", files=files, engine="python"), (
        "复扫给出基准答案"
    )


def test_form_feed_and_bom_and_lone_cr_do_not_split_the_two_engines(repo: Path) -> None:
    """`splitlines()` 还按 \\x0c/\\r/\\ufeff 等断行,rg 只按 \\n:让 rg 当行级权威就会给出错行号。

    这是"rg 只做文件级预筛、Python 是唯一行级实现"的理由,用例把结论钉住
    (分页符 \\x0c 在真实 Python 源码里并不罕见,行号错一位补丁就贴不上)。
    """
    _write(repo, "weird.py", "page\x0cparse_date after form feed\nsecond line\n")
    _write(repo, "cr.py", "parse_date old\rcr line\n")
    (repo / "bom.py").write_bytes("def parse_date(v):\n    return v\n".encode("utf-8-sig"))
    files = _iter_repo_files(repo, None)
    for needle in ("parse_date", "form feed", "cr line"):
        py = grep_files(repo, needle, files=files, engine="python")
        rg = grep_files(repo, needle, files=files, engine="rg")
        assert py == rg, needle
    weird = grep_files(repo, "parse_date", files=["weird.py"], engine="python")[0]
    assert [item["line"] for item in weird] == [2], "行号按 Python 的行模型(\\x0c 断行),不是 rg 的"
    assert "form feed" in weird[0]["text"]
    assert grep_files(repo, "parse_date", files=["weird.py"], engine="rg") == (weird, False)


def test_fallback_when_rg_binary_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """二进制缺失不是错误:引擎退回 python,结果照常给。"""
    _write(tmp_path, "src/app.py", APP_TEXT)
    monkeypatch.setattr("app.tools.search._rg_binary", lambda: None)
    meta: dict[str, Any] = {}
    matches, truncated = grep_files(
        tmp_path, "parse_date", files=["src/app.py"], engine="auto", meta=meta
    )
    assert meta["engine"] == "python" and matches[0]["line"] == 4 and truncated is False
    assert resolve_engine("auto") == "python" and resolve_engine("rg") == "python"


def test_timeout_falls_back_to_python(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时只让路径变慢,不让结果变化:kill 之后照旧由 Python 定案。

    `_rg_binary` 必须一起钉住:这台机器没装 rg 时 `resolve_engine("rg")` 直接返回
    python,rg 分支根本不被调用,`meta["fallback"]` 也就无从写起——ubuntu CI 上
    这条用例就是这样以 KeyError('fallback') 红的(它验证的是"rg 跑了但超时",
    不是"rg 装没装")。钉住探测结果,两条平台都能真正执行到被测逻辑。
    """
    import subprocess

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=["rg"], timeout=1)

    monkeypatch.setattr("app.tools.search._rg_binary", lambda: "/usr/local/bin/rg-under-test")
    monkeypatch.setattr("app.tools.search._run_rg", _boom)
    meta: dict[str, Any] = {}
    files = _iter_repo_files(repo, None)
    rg = grep_files(repo, "parse_date", files=files, engine="rg", meta=meta)
    assert meta["engine"] == "python" and "timeout" in meta["fallback"]
    assert rg == grep_files(repo, "parse_date", files=files, engine="python")


@needs_rg
def test_prefilter_cannot_widen_the_candidate_set(repo: Path) -> None:
    """被 MAX_LIST_FILES 裁掉的文件不得因为"rg 也看得见"就回到结果里。

    候选集的唯一出处是调用方给的 `files`(= `_iter_repo_files`);加速路径没有资格**新增**
    文件集合,否则模型会拿到 list_files 都列不出来的路径,预算门禁也变成摆设。
    """
    for index in range(520):
        _write(repo, f"pkg/f{index:03d}.py", "def filler():\n    return 1\n")
    _write(repo, "zzz_beyond_cap.py", "needle_beyond_the_cap here\n")
    files = _iter_repo_files(repo, None)
    assert len(files) == 500 and "zzz_beyond_cap.py" not in files, (
        "上限确实生效,越界文件不在扫描范围"
    )
    for engine in ("python", "rg"):
        assert grep_files(repo, "needle_beyond_the_cap", files=files, engine=engine) == (
            [],
            False,
        ), engine
    inside = grep_files(repo, "def filler", files=files, engine="rg")
    assert inside == grep_files(repo, "def filler", files=files, engine="python")
    assert inside[0], "上限内的文件照常命中(预筛没有把结果一起裁掉)"


def test_arg_batches_split_without_losing_or_duplicating_files() -> None:
    """参数表分批只改"分几次 subprocess",不改集合:不重、不漏、每批都在预算内。"""
    from app.tools.search import _RG_ARGV_LIMIT, _arg_batches

    files = [f"pkg/deep/path/to/an/a/file_{index:05d}.py" for index in range(3000)]
    batches = _arg_batches("rg", "needle", files)
    assert len(batches) > 1, "3000 个长路径必须切批"
    assert [rel for batch in batches for rel in batch] == files, "顺序与集合都不许变"
    for batch in batches:
        assert sum(len(rel) + 3 for rel in batch) + 48 + len("rg") + len("needle") <= _RG_ARGV_LIMIT


@needs_rg
def test_multi_batch_prefilter_still_equals_the_python_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把预算压到极小逼出多批:多进程并集必须与单批、与纯 Python 完全一致。"""
    monkeypatch.setattr("app.tools.search._RG_ARGV_LIMIT", 220)
    files = _iter_repo_files(repo, None)
    meta: dict[str, Any] = {}
    rg = grep_files(repo, "parse_date", files=files, engine="rg", context_lines=1, meta=meta)
    assert meta["engine"] == "rg" and "fallback" not in meta
    assert rg == grep_files(repo, "parse_date", files=files, engine="python", context_lines=1)


# ---------- Python 路径的绝对形状(不依赖 rg,任何机器都必须跑) ----------


def test_python_engine_default_shape_is_unchanged(repo: Path) -> None:
    """默认参数的绝对输出:键集合、顺序、trim、SKIP_DIRS / gitignore / hidden / 二进制口径。"""
    files = _iter_repo_files(repo, None)
    matches, truncated = grep_files(repo, "parse_date", files=files)
    assert truncated is False
    paths = [item["path"] for item in matches]
    assert not any(
        seg in {".venv", "node_modules", ".git", "__pycache__"}
        for rel in paths
        for seg in rel.split("/")
    ), "SKIP_DIRS 只按仓库内相对段跳过"
    assert "ignored.py" in paths, ".gitignore 不得影响可见性(旧 rglob 口径)"
    assert ".dotdir/hidden.py" in paths, "点目录不得影响可见性(旧 rglob 口径)"
    assert "blob.bin" not in paths, "前 1KB 有 NUL 的文件不得进结果"
    assert all(set(item) == {"path", "line", "text"} for item in matches), "默认不出 before/after"
    assert all(item["text"] == item["text"].strip() for item in matches)

    order = {rel: index for index, rel in enumerate(files)}
    keys = [(order[item["path"]], item["line"]) for item in matches]
    assert keys == sorted(keys), "总顺序必须是(文件在遍历序列里的位置, 行号)升序"
    assert len(set(keys)) == len(keys), "同一条命中不得出现两次"


def test_python_engine_trims_and_keeps_literal_semantics(repo: Path) -> None:
    assert (
        grep_files(repo, "parse_date", files=["src/long.py"])[0][0]["text"]
        == ("parse_date " + "y" * 300)[:200]
    )
    # 字面模式:正则元字符按字面处理,不当模式解释
    literal = grep_files(repo, "def parse_date(", files=["src/app.py"])
    assert [item["line"] for item in literal[0]] == [4]
    assert grep_files(repo, r"\w+", files=["src/app.py"])[0] == []


def test_truncated_rule_is_the_legacy_loop_break(repo: Path) -> None:
    """N 条刚好达上限:旧实现在"下一个文件"的循环头 break 并报 truncated → 照此判定。"""
    _write(repo, "one.py", "needle a\nneedle b\n")
    _write(repo, "sub/two.py", "nothing\n")
    kept = [
        {"path": "one.py", "line": 1, "text": "needle a"},
        {"path": "one.py", "line": 2, "text": "needle b"},
    ]
    assert grep_files(repo, "needle", files=["one.py", "sub/two.py"], max_results=2) == (kept, True)
    assert grep_files(repo, "needle", files=["sub/two.py", "one.py"], max_results=2) == (
        kept,
        False,
    )
    assert grep_files(repo, "needle", files=["one.py", "sub/two.py"], max_results=1) == (
        kept[:1],
        True,
    )
    assert grep_files(repo, "needle", files=["one.py"], max_results=3) == (kept, False)
    assert grep_files(repo, "needle", files=[], max_results=0) == ([], False)
    assert grep_files(repo, "needle", files=["one.py"], max_results=0) == ([], True)


@needs_rg
def test_truncated_rule_matches_rg_engine(repo: Path) -> None:
    _write(repo, "one.py", "needle a\nneedle b\n")
    _write(repo, "sub/two.py", "nothing\n")
    for files in (["one.py", "sub/two.py"], ["sub/two.py", "one.py"]):
        for max_results in (0, 1, 2, 3):
            py = grep_files(repo, "needle", files=files, max_results=max_results, engine="python")
            rg = grep_files(repo, "needle", files=files, max_results=max_results, engine="rg")
            assert rg == py, (files, max_results)


# ---------- 新参数:context_lines / per_file_cap / regex ----------


def test_context_lines_shape_and_default_absence(repo: Path) -> None:
    ctx = _ctx(repo)
    plain = search_code(ctx, "parse_date")
    assert plain.ok
    assert all("before" not in item and "after" not in item for item in plain.output["matches"])
    assert plain.output["engine"] in ("rg", "python")

    widened = search_code(ctx, "parse_date", glob="src/app.py", context_lines=1)
    entry = widened.output["matches"][0]
    assert entry["line"] == 4
    assert entry["before"] == [""] and entry["after"] == ["return value"]
    assert widened.output["context_lines"] == 1


def test_context_lines_clamped_to_settings_ceiling(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要 10000 行是上下文爆炸,不是需求:夹扣到 Settings 天花板,并把实际值回给模型。"""
    _override(monkeypatch, max_search_context_lines=2)
    ctx = _ctx(repo)
    result = search_code(ctx, "parse_date", glob="src/app.py", context_lines=10000)
    assert result.ok and result.output["context_lines"] == 2
    assert len(result.output["matches"][0]["after"]) <= 2
    assert len(result.output["matches"][0]["before"]) <= 2

    negative = search_code(ctx, "parse_date", glob="src/app.py", context_lines=-5)
    assert negative.ok and "context_lines" not in negative.output
    assert "before" not in negative.output["matches"][0], "负数按 0 处理,与默认一致"

    closed = search_code(ctx, "parse_date", glob="src/app.py", context_lines=3)
    assert closed.output["context_lines"] == 2, "天花板对每一次调用生效,不是只裁第一次"


def test_context_lines_non_integer_is_named_back_to_the_model(repo: Path) -> None:
    bad = search_code(_ctx(repo), "parse_date", context_lines="lots")
    assert not bad.ok and "context_lines" in (bad.error or "")


def test_per_file_cap_spreads_results_across_files(repo: Path) -> None:
    ctx = _ctx(repo)
    capped = search_code(ctx, "parse_date", per_file_cap=1)
    assert capped.ok
    per_file: dict[str, int] = {}
    for item in capped.output["matches"]:
        per_file[item["path"]] = per_file.get(item["path"], 0) + 1
    assert per_file and all(count == 1 for count in per_file.values()), per_file
    assert len(per_file) > 1, "上限的意义是把结果摊开到多个文件,不是单纯砍总数"

    uncapped = search_code(ctx, "parse_date")
    assert len(uncapped.output["matches"]) > len(capped.output["matches"])
    assert len(uncapped.output["matches"]) == uncapped.output["total"]


def test_regex_mode_valid_and_invalid(repo: Path) -> None:
    ctx = _ctx(repo)
    bad = search_code(ctx, r"parse_date(", regex=True)
    assert not bad.ok
    assert r"parse_date(" in (bad.error or ""), "失败文本必须点名是哪个模式坏了"
    assert "regex" in (bad.error or "").lower()

    good = search_code(ctx, r"def parse_\w+\(", regex=True)
    assert good.ok
    texts = [item["text"] for item in good.output["matches"]]
    assert any(text.startswith("def parse_date(") for text in texts)
    assert any(text.startswith("def parse_datetime(") for text in texts)
    # 默认仍是字面子串:同一个模式在默认口径下一条不中
    assert search_code(ctx, r"def parse_\w+\(").output["total"] == 0


def test_keyword_and_cap_edges(repo: Path) -> None:
    ctx = _ctx(repo)
    assert not search_code(ctx, "   ").ok
    assert not search_code(ctx, "").ok
    small = _ctx(repo, max_search_results=3)
    result = search_code(small, "parse_date")
    assert result.output["total"] == 3 and result.output["truncated"] is True
    assert len(result.output["matches"]) == 3


# ---------- 引擎选择 ----------


@needs_rg
def test_search_engine_python_is_honoured_even_with_rg(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _override(monkeypatch, search_engine="python")
    assert search_code(_ctx(repo), "parse_date").output["engine"] == "python"
    assert resolve_engine("python") == "python"

    _override(monkeypatch, search_engine="auto")
    assert search_code(_ctx(repo), "parse_date").output["engine"] == "rg"
    assert resolve_engine("auto") == "rg"
    assert resolve_engine("rg") == "rg"


def test_search_settings_defaults_and_validation() -> None:
    defaults = Settings()
    assert defaults.search_engine == "auto"
    assert (defaults.max_search_context_lines, defaults.max_symbol_results) == (5, 30)
    assert Settings(search_engine="python").search_engine == "python"
    assert Settings(search_engine="rg").search_engine == "rg"
    with pytest.raises(ValueError, match="search_engine"):
        Settings(search_engine="grep")
    with pytest.raises(ValueError, match="max_search_context_lines"):
        Settings(max_search_context_lines=-1)
    with pytest.raises(ValueError, match="max_symbol_results"):
        Settings(max_symbol_results=0)


# ---------- describe_file ----------


def test_describe_file_lists_classes_methods_and_line_ranges(repo: Path) -> None:
    _write(repo, "pkg/symbols.py", DESCRIBE_SRC)
    result = describe_file(_ctx(repo), "pkg/symbols.py")
    assert result.ok, result.error
    out = result.output
    assert out["parse_error"] is None
    assert out["total_lines"] == len(DESCRIBE_SRC.splitlines())
    symbols = out["symbols"]
    assert [item["name"] for item in symbols] == [
        "top",
        "Outer",
        "Inner",
        "deep",
        "method",
        "afetch",
        "fetch",
    ]
    assert [item["kind"] for item in symbols] == [
        "function",
        "class",
        "class",
        "method",
        "method",
        "method",
        "function",
    ]
    assert all(
        set(item) == {"name", "kind", "start_line", "end_line", "signature"} for item in symbols
    )
    assert symbols[0]["signature"] == "@decorator def top(value, other=3) -> int"
    assert (symbols[0]["start_line"], symbols[0]["end_line"]) == (5, 6)
    assert (symbols[1]["start_line"], symbols[1]["end_line"]) == (9, 21)
    assert all(item["start_line"] <= item["end_line"] for item in symbols)
    assert "local" not in {item["name"] for item in symbols}, "函数体内的局部函数不是仓库结构"


def test_describe_file_methods_carry_their_own_line_ranges(repo: Path) -> None:
    _write(
        repo,
        "pkg/cls.py",
        "class Service:\n    def run(self):\n        return 1\n\n    def stop(self):\n        return 2\n",
    )
    symbols = describe_file(_ctx(repo), "pkg/cls.py").output["symbols"]
    assert [(item["name"], item["kind"], item["start_line"]) for item in symbols] == [
        ("Service", "class", 1),
        ("run", "method", 2),
        ("stop", "method", 5),
    ]
    assert symbols[0]["end_line"] == 6 and symbols[1]["end_line"] == 3


def test_describe_file_parse_error_is_a_usable_result(repo: Path) -> None:
    """坏文件给出可用结果,不是一次崩溃也不是死路:模型据此改用 search_code/read_file。"""
    _write(repo, "pkg/broken.py", "def broken(:\n    pass\n")
    result = describe_file(_ctx(repo), "pkg/broken.py")
    assert result.ok, result.error
    assert result.output["symbols"] == [] and result.output["count"] == 0
    assert "SyntaxError" in result.output["parse_error"]
    assert result.output["total_lines"] == 2

    missing = describe_file(_ctx(repo), "pkg/missing.py")
    assert not missing.ok and "not found or outside workspace" in (missing.error or "")


def test_describe_file_rejects_binary_non_python_and_escapes(repo: Path) -> None:
    ctx = _ctx(repo)
    (repo / "pkg" / "blob.py").parent.mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "blob.py").write_bytes(BINARY)
    _write(repo, "notes.txt", "def not_python_symbol():\n    pass\n")
    assert "binary" in (describe_file(ctx, "pkg/blob.py").error or "")
    assert "only parses Python" in (describe_file(ctx, "notes.txt").error or "")
    for escape in ("../outside.py", str(repo.parent / "x.py"), ".git/config/../../evil.py"):
        assert not describe_file(ctx, escape).ok, escape


# ---------- find_symbol ----------


def test_find_symbol_prefers_exact_over_prefix(repo: Path) -> None:
    _write(
        repo,
        "pkg/dates.py",
        "def parse_datetime(v):\n    return v\n\n\ndef parse_date_range(a, b):\n    return a\n",
    )
    _write(repo, "pkg/target.py", "def parse_date(v):\n    return v\n")
    result = find_symbol(_ctx(repo), "parse_date")
    assert result.ok, result.error
    assert result.output["match"] == "exact"
    # 精确名有两处(pkg/target.py 与 fixture 自带的 src/app.py);前缀名一概不出现
    assert [(item["name"], item["path"]) for item in result.output["matches"]] == [
        ("parse_date", "pkg/target.py"),
        ("parse_date", "src/app.py"),
    ], "精确名命中时不得被 parse_datetime / parse_date_range 淹没"
    assert result.output["matches"][0]["line"] == 1
    assert result.output["matches"][0]["signature"] == "def parse_date(v)"
    assert set(result.output["matches"][0]) == {"name", "kind", "path", "line", "signature"}
    assert result.output["total"] == 2 and result.output["truncated"] is False

    prefix = find_symbol(_ctx(repo), "parse_date_r")
    assert prefix.output["match"] == "prefix"
    assert [item["name"] for item in prefix.output["matches"]] == [
        "parse_date_range",
        "parse_date_range",
    ]
    assert [item["path"] for item in prefix.output["matches"]] == ["pkg/dates.py", "src/util.py"]

    none = find_symbol(_ctx(repo), "zzzz_absent")
    assert none.ok and none.output["match"] == "none" and none.output["matches"] == []
    assert not find_symbol(_ctx(repo), "  ").ok


def test_find_symbol_kind_filter(repo: Path) -> None:
    _write(
        repo,
        "pkg/kinds.py",
        "class Widget:\n    def widget(self):\n        return 1\n\n\ndef widget():\n    return 2\n",
    )
    ctx = _ctx(repo)
    assert {
        (item["kind"], item["line"]) for item in find_symbol(ctx, "widget").output["matches"]
    } == {
        ("class", 1),
        ("method", 2),
        ("function", 6),
    }
    methods = find_symbol(ctx, "widget", kind="method").output["matches"]
    assert [item["kind"] for item in methods] == ["method"] and methods[0]["line"] == 2
    classes = find_symbol(ctx, "widget", kind="class").output["matches"]
    assert [item["kind"] for item in classes] == ["class"]
    bad = find_symbol(ctx, "widget", kind="variable")
    assert not bad.ok and "unknown kind" in (bad.error or "")


def test_find_symbol_ordering_is_deterministic(repo: Path) -> None:
    for rel in ("pkg/z_last.py", "pkg/a_first.py", "pkg/mid/b_middle.py"):
        _write(repo, rel, "def shared_name():\n    return 1\n")
    ctx = _ctx(repo)
    paths = [item["path"] for item in find_symbol(ctx, "shared_name").output["matches"]]
    assert paths == ["pkg/a_first.py", "pkg/mid/b_middle.py", "pkg/z_last.py"], "结果按路径确定序"
    assert [item["path"] for item in find_symbol(ctx, "shared_name").output["matches"]] == paths


def test_find_symbol_cap_and_truncated(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _override(monkeypatch, max_symbol_results=2)
    for index in range(5):
        _write(repo, f"pkg/m{index}.py", f"def repeated_name():\n    return {index}\n")
    result = find_symbol(_ctx(repo), "repeated_name")
    assert result.output["truncated"] is True
    assert result.output["total"] == 2 and len(result.output["matches"]) == 2
    assert [item["path"] for item in result.output["matches"]] == ["pkg/m0.py", "pkg/m1.py"]


def test_find_symbol_ignores_skip_dirs_hidden_binaries_and_broken(repo: Path) -> None:
    _write(repo, "pkg/bad.py", "def broken_symbol(:\n")
    _write(repo, "plain.txt", "def not_python_symbol():\n    pass\n")
    ctx = _ctx(repo)
    assert find_symbol(ctx, "broken_symbol").output["match"] == "none", "解析失败的文件不参与索引"
    assert find_symbol(ctx, "not_python_symbol").output["match"] == "none"
    assert find_symbol(ctx, "parse_date_in_venv").output["match"] == "none", (
        "SKIP_DIRS 里的定义查不到"
    )
    assert find_symbol(ctx, "parse_date").output["total"] == 1, ".venv/node_modules 不进符号索引"


def test_find_symbol_skips_symlinked_files_when_the_flag_is_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这台 Windows 没有建软链的权限,就用替身证明代码确实问了 is_symlink 并据此跳过。"""
    _write(repo, "pkg/real.py", "def link_only_symbol():\n    return 1\n")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: Path(self).name == "real.py" or real_is_symlink(self)
    )
    assert find_symbol(_ctx(repo), "link_only_symbol").output["match"] == "none"


def test_rg_engine_defers_to_python_when_a_symlink_is_in_scope(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rg 对显式文件参数的跟随语义无契约可依 → 见到软链就整批让给 Python(基准路径)。

    同样必须钉住 `_rg_binary`:这台机器没有 rg 时 `resolve_engine("rg")` 直接返回
    python,`_rg_files` 不被调用,软链让路那段代码一行都没执行(ubuntu CI 上以
    KeyError('fallback') 红)。替身只回答"rg 在不在",被测的让路逻辑照原样跑。
    """
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr("app.tools.search._rg_binary", lambda: "/usr/local/bin/rg-under-test")
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: "util" in Path(self).as_posix() or real_is_symlink(self)
    )
    files = _iter_repo_files(repo, None)
    meta: dict[str, Any] = {}
    rg = grep_files(repo, "parse_date", files=files, engine="rg", meta=meta)
    assert meta["engine"] == "python" and "symlink" in meta["fallback"]
    assert rg == grep_files(repo, "parse_date", files=files, engine="python")


def test_find_symbol_skips_symlinks_and_stays_in_workspace(tmp_path: Path) -> None:
    """仓库外一个同名定义不得进结果;仓库内指向外部的符号链接也不得被解析。"""
    root = tmp_path / "repo"
    _write(root, "pkg/inside.py", "def escaped_name():\n    return 1\n")
    _write(tmp_path, "outside.py", "def outside_only():\n    return 1\n")
    outside_target = tmp_path / "outside.py"
    link = root / "pkg" / "link.py"
    try:
        link.symlink_to(outside_target)
    except OSError:
        pytest.skip("这台 Windows 没有创建符号链接的权限")
    ctx = _ctx(root)
    assert find_symbol(ctx, "outside_only").output["matches"] == [], (
        "跟随仓库内软链=给 workspace 外开了口子"
    )
    assert find_symbol(ctx, "escaped_name").output["total"] == 1
    assert not describe_file(ctx, "pkg/link.py").ok, (
        "越出 workspace 的符号链接必须被 relpath_within 拒掉"
    )


# ---------- read_file 的 limit ----------


def test_read_file_limit_windows_and_ceiling(repo: Path) -> None:
    ctx = _ctx(repo)
    full = read_file(ctx, "src/app.py")
    assert full.ok and full.output["returned_lines"] == 5 and full.output["truncated"] is False
    assert set(full.output) == {
        "path",
        "total_lines",
        "offset",
        "returned_lines",
        "content",
        "truncated",
    }, "不传 limit 时输出键集合与旧实现逐字一致"

    windowed = read_file(ctx, "src/app.py", offset=3, limit=2)
    assert windowed.output["offset"] == 3
    assert windowed.output["returned_lines"] == 2
    assert windowed.output["content"] == "\ndef parse_date(value):"
    assert windowed.output["truncated"] is True

    tight = _ctx(repo, max_read_lines=3)
    assert read_file(tight, "src/app.py", 1, 100).output["returned_lines"] == 3, "天花板在 Settings"
    assert read_file(tight, "src/app.py", 1).output["returned_lines"] == 3
    assert read_file(tight, "src/app.py", 1).output["truncated"] is True
    assert read_file(tight, "src/app.py", 5, 10).output["returned_lines"] == 1

    assert not read_file(ctx, "src/app.py", 1, 0).ok
    assert not read_file(ctx, "src/app.py", 1, "abc").ok
    assert not read_file(ctx, "../outside.py").ok


# ---------- 阶段白名单与边界守卫 ----------


def test_new_retrieval_tools_are_allowed_in_both_phases() -> None:
    """LOCALIZE 与 PROPOSE 都要能用符号工具:定位段查定义,补丁段确认邻居。

    消融臂 `app/evals/single_shot.py` import 的是同一个 READ_TOOLS 对象,
    ONE_SHOT_TOOLS 也同步加了这两个名字——两臂的检索能力必须等量,否则对照的是残臂。
    """
    for name in ("find_symbol", "describe_file"):
        assert name in READ_TOOLS, name
        assert name in WRITE_TOOLS, name
    from app.evals.single_shot import ONE_SHOT_TOOLS

    assert {"find_symbol", "describe_file"} <= set(ONE_SHOT_TOOLS)
    assert "run_tests" not in ONE_SHOT_TOOLS, "消融点不许被顺手改回去"


def test_retrieval_tools_cannot_read_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/app.py", "def inside():\n    return 1\n")
    _write(
        tmp_path,
        "outside.py",
        "secret = parse_date_outside\n\n\ndef outside_only():\n    return 1\n",
    )
    ctx = _ctx(root)
    assert list(_iter_repo_files(root, None)) == ["src/app.py"], "遍历范围就是 workspace"
    for path in ("../outside.py", "/outside.py", "src/../../outside.py", "./../outside.py"):
        assert not describe_file(ctx, path).ok, path
        assert not read_file(ctx, path).ok, path
    assert search_code(ctx, "parse_date_outside").output["total"] == 0
    assert find_symbol(ctx, "outside_only").output["matches"] == []


def test_registry_schemas_carry_the_new_params() -> None:
    from app.tools.registry import REGISTRY, tool_schemas

    search_props = REGISTRY["search_code"].parameters["parameters"]["properties"]
    assert {"keyword", "glob", "context_lines", "per_file_cap", "regex"} <= set(search_props)
    assert REGISTRY["search_code"].parameters["parameters"]["required"] == ["keyword"]
    assert {"path", "offset", "limit"} <= set(
        REGISTRY["read_file"].parameters["parameters"]["properties"]
    )
    assert REGISTRY["find_symbol"].parameters["parameters"]["required"] == ["name"]
    assert set(REGISTRY["describe_file"].parameters["parameters"]["properties"]) == {"path"}
    assert "class" in REGISTRY["find_symbol"].parameters["parameters"]["properties"]["kind"]["enum"]
    assert {"find_symbol", "describe_file"} <= {item["name"] for item in tool_schemas()}
    # 单入口证明的措辞不得被顺手改掉
    assert "unified diff 会被直接拒" in REGISTRY["apply_patch"].description
