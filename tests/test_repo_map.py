"""M2 仓库骨架(持久记忆)用例:纯本地渲染 + 阶段接线,零网络、零真实模型。

钉住:大纲形状与真实行号(模块级/类/嵌套类/async/装饰器、ast.arguments 签名重建)、
降级口径(语法错与非 UTF-8/二进制只进树、SKIP_DIRS 与 list_files 同集合)、截断可见性
(超预算必发 truncated 行、绝不发半截行、长度不超 max_chars)、接线边界(关闭时系统提示
逐字不变,且只有 localize/propose 两个调用点接骨架)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.context.repo_map import build_repo_map, repo_map_for_workspace
from app.gitops.snapshot import create_workspace
from app.graph.nodes import READ_TOOLS, WRITE_TOOLS, TaskNodes
from app.graph.plain_loop import LoopOutcome, run_plain_loop
from app.llm.fake import FakeLLM
from app.prompts import SYSTEM_PROMPT
from app.tools.base import ToolContext
from app.tools.files import SKIP_DIRS
from app.tools.tracker import Tracker

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUE = "空字符串未防御,应返回 None"
FINISH = [{"tool": "finish", "args": {"success": True, "summary": "根因在空值分支"}}]
STATE: dict[str, object] = {
    "round_no": 1,
    "tokens_used": 0,
    "turns": 0,
    "issue_text": ISSUE,
    "findings": "",
}

SYMBOLS_SRC = '''"""模块 docstring。"""


@decorator
def top(value, other=3) -> int:
    return value


class Outer:
    """外层。"""

    class Inner:
        def deep(self):
            pass

    def method(self, a, *, b=1, **kwargs):
        def local():
            pass
        return a


async def fetch(url: str, *, timeout=5.0) -> dict[str, int]:
    return {}
'''

SIG_SRC = """def full(a, b=1, /, c=2, *args, d, e=3, **kwargs) -> list[int]: ...


def quoter(x=None, y='s', z=(1, 2)) -> None: ...


def bare(*, flag, other=1): ...
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_outline_shape_and_exact_line_numbers(tmp_path: Path) -> None:
    """行号取 ast 的 lineno/end_lineno:装饰器算进 def 的起点;函数体内局部函数不出。"""
    _write(tmp_path, "pkg/symbols.py", SYMBOLS_SRC)
    out = build_repo_map(tmp_path, max_chars=4000)

    assert out.startswith("<repo_skeleton>")
    assert out.splitlines()[1:] == [
        "pkg/symbols.py",
        "pkg/symbols.py [lines 23]",
        "@decorator def top(value, other=3) -> int (L5-L6)",
        "class Outer (L9-L19)",
        "  class Inner (L12-L14)",
        "    def deep(self) (L13-L14)",
        "  def method(self, a, *, b=1, **kwargs) (L16-L19)",
        "async def fetch(url, *, timeout=5.0) -> dict[str, int] (L22-L23)",
    ]
    assert "def local" not in out


def test_signature_reconstruction(tmp_path: Path) -> None:
    """签名从 ast.arguments 重建:posonly `/`、默认值、*args、裸 *、kwonly、**kwargs、返回注解。"""
    _write(tmp_path, "sig.py", SIG_SRC)
    out = build_repo_map(tmp_path, max_chars=4000)

    assert "def full(a, b=1, /, c=2, *args, d, e=3, **kwargs) -> list[int] (L1-L1)" in out
    assert "def quoter(x=None, y='s', z=(1, 2)) -> None (L4-L4)" in out
    assert "def bare(*, flag, other=1) (L7-L7)" in out


def test_syntax_error_degrades_and_only_parseable_python_is_outlined(tmp_path: Path) -> None:
    """坏文件只进树、同批好文件照常出符号;非 .py / 二进制 / 无符号的 .py 都只进树。"""
    _write(tmp_path, "pkg/bad.py", "def broken(:\n")
    _write(tmp_path, "pkg/good.py", "def steady(value): ...\n")
    _write(tmp_path, "plain.txt", "def fake_symbol():\n    pass\n")
    _write(tmp_path, "data/consts.py", "LIMIT = 3\n")
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01binary")
    out = build_repo_map(tmp_path, max_chars=4000)
    lines = out.splitlines()

    for rel in ("pkg/bad.py", "plain.txt", "blob.dat", "data/consts.py"):
        assert rel in lines
    assert "pkg/bad.py [lines" not in out and "data/consts.py [lines" not in out
    assert "def fake_symbol" not in out  # 非 .py 文件不解析
    assert "def steady(value) (L1-L1)" in out


def test_skip_dirs_matches_list_files_scope(tmp_path: Path) -> None:
    """SKIP_DIRS 复用 app.tools.files:骨架与 list_files 对"仓库里有什么"不能两样。"""
    hidden = (".venv/lib/pkg.py", "__pycache__/cached.py", "node_modules/p/index.js", ".git/config")
    for rel in hidden:
        _write(tmp_path, rel, "def hidden_symbol(): ...\n")
    _write(tmp_path, "src/visible.py", "def shown(): ...\n")
    out = build_repo_map(tmp_path, max_chars=4000)

    assert "def hidden_symbol" not in out and "def shown() (L1-L1)" in out
    for line in out.splitlines()[1:]:
        assert not any(seg in SKIP_DIRS for seg in line.split("/")), line


def test_caps_and_degenerate_budgets(tmp_path: Path) -> None:
    """max_files 溢出改出目录汇总(M2.5);max_chars<=0 关闭;空仓库出短块;预算过小宁可不发。"""
    for index in range(8):
        _write(tmp_path, f"pkg/m{index}.py", f"def f{index}(): ...\n")
    # 形状变更是**有意为之**(理由与实测证据见 PROGRESS.md 的 M2.5 与 D.12):超过取样上限时,
    # 旧口径列"字母序前 N 个文件 + omitted 计数",会让同一个目录里剩下的文件凭空消失;
    # 新口径改出目录级汇总(行数由目录数决定),并如实说明大纲只覆盖了取样集。
    capped = build_repo_map(tmp_path, max_chars=4000, max_files=3).splitlines()
    assert capped[1] == "pkg/  (8 files, 8 py)"
    assert capped[-1].startswith("… 8 files in repo; directory rollup covers all of them")
    assert "per-file outlines shown for a 3-file sample" in capped[-1]
    assert build_repo_map(tmp_path, max_chars=0) == ""
    assert build_repo_map(tmp_path, max_chars=-5) == ""
    assert "Repository is empty." in build_repo_map(tmp_path / "none", max_chars=4000)


def test_truncation_is_visible_and_never_splits_a_line(tmp_path: Path) -> None:
    for index in range(8):
        _write(tmp_path, f"pkg/module{index}.py", f"def fn_{index}(value): ...\n")
    complete = build_repo_map(tmp_path, max_chars=4000)
    known = set(complete.splitlines())
    assert "truncated" not in complete, "装得下时不得谎报截断"

    for max_chars in (150, 220, 320, 480):
        cut = build_repo_map(tmp_path, max_chars=max_chars)
        if cut == "":  # 连"头部 + 截断行"都放不下:宁可不发,也不发误导性的半份信息
            continue
        assert len(cut) <= max_chars
        note = cut.splitlines()[-1]
        assert note.startswith("… skeleton truncated: ") and note.endswith(" files omitted")
        for line in cut.splitlines():  # 每行都必须完整:骨架里的整行,或截断行本身
            assert line in known or line == note, line


def test_wrapper_never_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """骨架是优化项:遍历炸了(权限/坏目录/实现缺陷)也只能退化,不得带走任务。"""
    from app.context import repo_map as module

    def _boom(self: Path, pattern: str) -> list[Path]:
        raise OSError("rglob boom")

    monkeypatch.setattr(module.Path, "rglob", _boom)
    assert repo_map_for_workspace(tmp_path, max_chars=4000) == ""


def test_settings_limits_validated() -> None:
    defaults = Settings()
    assert defaults.repo_map_enabled is True, "默认开启:结构先验是常态,关闭是例外"
    assert (defaults.repo_map_max_chars, defaults.repo_map_max_files) == (4000, 200)
    with pytest.raises(ValueError, match="repo_map_max_chars"):
        Settings(repo_map_max_chars=-1)
    with pytest.raises(ValueError, match="repo_map_max_files"):
        Settings(repo_map_max_files=0)


# ---------- 接线:持久记忆进 LOCALIZE / PROPOSE ----------


class _RecordingFakeLLM(FakeLLM):
    """FakeLLM + 记下每次收到的 messages:骨架是否真进系统提示只能对着请求核。"""

    def __init__(self, script: list[dict]) -> None:
        super().__init__(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append([dict(m) for m in messages])
        return super().complete(messages, tools)


@pytest.fixture()
def ws_ctx(demo_repo: Path, tmp_path: Path) -> ToolContext:
    ws = tmp_path / "ws"
    return ToolContext("T-M2", ws, create_workspace(demo_repo, ws), Tracker(tmp_path / "t.jsonl"))


def _nodes(
    ws_ctx: ToolContext,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, object]],
) -> TaskNodes:
    """捕获 run_plain_loop 入参的 TaskNodes:骨架接线只允许动 extra_system。"""

    def _loop(*args: object, **kwargs: object) -> LoopOutcome:
        calls.append(kwargs)
        return LoopOutcome(True, "根因在空值分支", 1, 0, False, True)

    monkeypatch.setattr("app.graph.nodes.get_settings", lambda: settings)
    monkeypatch.setattr("app.graph.nodes.run_plain_loop", _loop)
    return TaskNodes(
        bug=SimpleNamespace(issue_text=ISSUE, failed_tests=[]),  # type: ignore[arg-type]
        model=SimpleNamespace(provider="fake-replay"),  # type: ignore[arg-type]
        workspace=ws_ctx.workspace,
        tracker=ws_ctx.tracker,
        report_dir=ws_ctx.report_dir,
        max_rounds=5,
        max_turns=10,
        ctx=ws_ctx,
    )


def _wired_nodes(
    ws_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch, **repo_map_kw: object
) -> list[dict[str, object]]:
    """跑一遍 localize + propose,返回两段实际收到的循环入参。

    额度显式钉住(不随本机 .env 漂移):localize 拿 200000×0.6=120000,propose 拿整份。
    """
    calls: list[dict[str, object]] = []
    settings = Settings(token_budget=200_000, localize_budget_share=0.6, **repo_map_kw)
    nodes = _nodes(ws_ctx, settings, monkeypatch, calls)
    nodes.localize(STATE)  # type: ignore[arg-type]
    nodes.propose(STATE)  # type: ignore[arg-type]
    assert [kwargs["state_label"] for kwargs in calls] == ["LOCALIZE", "PROPOSE_PATCH"]
    return calls


def test_skeleton_reaches_model_system_message(ws_ctx: ToolContext) -> None:
    """开启时:骨架在第一条 system 里,且带得出 demo_repo 的真实符号名。"""
    extra = repo_map_for_workspace(ws_ctx.workspace, max_chars=4000)
    assert "<repo_skeleton>" in extra and "def parse_date(" in extra

    model = _RecordingFakeLLM(FINISH)
    run_plain_loop(ws_ctx, model, ISSUE, extra_system=extra)

    assert model.seen[0][0]["content"] == SYSTEM_PROMPT + "\n\n" + extra


def test_localize_and_propose_get_skeleton_without_touching_gates(
    ws_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _wired_nodes(ws_ctx, monkeypatch)
    for kwargs in calls:
        extra = kwargs["extra_system"]
        assert isinstance(extra, str) and "def parse_date(" in extra
    # 工具白名单与预算口径未被接线影响
    assert calls[0]["allowed_tools"] == READ_TOOLS and calls[1]["allowed_tools"] == WRITE_TOOLS
    assert calls[0]["token_budget"] == 120_000 and calls[1]["token_budget"] == 200_000


def test_disabled_is_byte_identical_to_pre_change(
    ws_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归钉子:关闭骨架时 extra_system 是空串,系统提示逐字节等于改动前。"""
    calls = _wired_nodes(ws_ctx, monkeypatch, repo_map_enabled=False)
    assert [kwargs["extra_system"] for kwargs in calls] == ["", ""]

    model = _RecordingFakeLLM(FINISH)
    run_plain_loop(ws_ctx, model, ISSUE, extra_system=calls[0]["extra_system"])
    assert model.seen[0][0]["content"] == SYSTEM_PROMPT


def test_branch_candidate_call_site_still_takes_only_the_variant_hint() -> None:
    """规格红线:骨架只接 localize/plan/propose 三处,自适应候选点原样(只带换思路提示)。

    M5 把 plan 也接上(计划要点名文件与符号,只靠单薄的定位结论写不出可执行的计划);
    候选点仍不接,是为了让分支对照保持"只差变体提示"的单变量形状。
    """
    source = (REPO_ROOT / "app/graph/nodes.py").read_text(encoding="utf-8").splitlines()
    hits = [line.strip() for line in source if "extra_system=" in line]
    assert hits == [
        "extra_system=self._persistent_context(self.workspace),",
        "extra_system=self._persistent_context(self.workspace),",
        "extra_system=self._persistent_context(self.workspace),",
        'extra_system=VARIANT_HINTS.get(index, ""),',
    ]
