"""P3-7 防复发机制:文档锚点断言(file:line 级)。

背景(interview-audit-2026-09-26.md R3-Q1):夜间 GOAL 模式改代码时周边权威
文档没有同步修订,产出 7 处与代码互斥的假命题,且无任何机制强制一致。
本测试把「文档句」钉在 文件 + 期望子串 的锚点上:

1. 每篇 ADR 末尾必须有「验证锚点」节,逐条声明 `路径` — `期望子串`;
2. 锚点所指文件必须仍包含期望子串(文案回退或实现被删都算失败)。
   2026-10-11 起不再钉行号:行号锚点每插一行代码就漂移,历史上约 30 个 commit
   只是在"重钉行号",没有一次抓到真实的文档/代码矛盾;旧的 `路径:行号` 写法当场报错;
3. 已知的假命题文案禁止在任何文档/源码中复活(黑名单);
4. README 的能力清单(工具条数与名单)以代码里的注册表为准绳逐名对齐;
5. 依赖声明以 `pyproject.toml` 为唯一事实源,`uv.lock` 必须入库且与之同步;
6. 提交进仓库的生成物(interview-guide 站点 HTML)必须与它的 Markdown 事实源逐字节一致。

夜间 GOAL 的「全量 pytest 绿」红线由此物理卡住"改代码不改文档"。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from app.tools.registry import FINISH_TOOL, REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]

# ADR 验证锚点节的条目格式:`- `相对路径` — `期望子串``
_ANCHOR_LINE = re.compile(r"^- `([^`\s:]+)` — `([^`]+)`$")
# 锚点节里以反引号路径开头的条目都必须符合上面的格式——不合规的条目以前会被静默跳过
_ANCHOR_LIKE = re.compile(r"^- `[^`\s]+` — ")
_ANCHOR_SECTION = "## 验证锚点"


def _read(relpath: str) -> list[str]:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8").splitlines()


def _iter_anchor_entries() -> list[tuple[str, str, str]]:
    """从全部 ADR 的验证锚点节收集 (来源 ADR, 路径, 期望子串)。"""
    entries: list[tuple[str, str, str]] = []
    for adr in sorted((REPO_ROOT / "docs" / "adr").glob("*.md")):
        text = adr.read_text(encoding="utf-8")
        assert _ANCHOR_SECTION in text, f"{adr.name} 缺少「{_ANCHOR_SECTION}」节"
        section = text.split(_ANCHOR_SECTION, 1)[1]
        rel = adr.relative_to(REPO_ROOT).as_posix()
        found: list[tuple[str, str]] = []
        for line in section.splitlines():
            if not _ANCHOR_LIKE.match(line):
                continue
            match = _ANCHOR_LINE.match(line)
            assert match, (
                f"{adr.name} 的锚点条目格式不对(只写 `路径` — `期望子串`,不带行号):{line!r}"
            )
            found.append((match.group(1), match.group(2)))
        assert found, f"{adr.name} 的验证锚点节为空(至少声明一条锚点)"
        entries.extend((rel, path, expected) for path, expected in found)
    return entries


def test_adr_anchor_sections_exist_with_entries() -> None:
    """每篇 ADR 都有非空的验证锚点节(新 ADR 入库时必须声明锚点)。"""
    adrs = sorted((REPO_ROOT / "docs" / "adr").glob("*.md"))
    assert adrs, "docs/adr/ 下没有任何 ADR"
    entries = _iter_anchor_entries()
    assert len(entries) >= len(adrs), "至少每篇 ADR 一条锚点"


def test_adr_declared_anchors_hold() -> None:
    """ADR 声明的每条锚点:所指文件存在、且仍包含期望子串。"""
    for source, relpath, expected in _iter_anchor_entries():
        path = REPO_ROOT / relpath
        assert path.is_file(), f"锚点失配:{source} 声明的 {relpath} 不存在"
        assert expected in path.read_text(encoding="utf-8"), (
            f"锚点失配:{source} 声明 {relpath} 应含 {expected!r},文件里已找不到"
            "(文档或实现改动后必须同步修订锚点)"
        )


def test_p3_7_false_claims_are_gone() -> None:
    """已对账的假命题文案黑名单:不得在文档/源码中复活。"""
    forbidden: dict[str, list[str]] = {
        # 「checkpoint 带来崩溃恢复」与 checkpoint.py:3-5 自述互斥(R3-Q1)
        # design.md 已归档到 docs/archive/(docs 分层重组),黑名单作用域跟着换路径,
        # 否则这两条假命题在新位置复活时无人拦截
        "免费获得节点级恢复": ["docs/adr", "docs/archive/design.md", "docs", "app", "README.md"],
        "带来崩溃恢复": ["docs/adr", "docs/archive/design.md"],
        # ATTACK-009 新增后「8 个」未回改,实测 9 个(R3-Q1)
        "攻击样例 8 个拦截": ["docs/threat-model.md"],
        "8 个攻击样例": ["README.md"],
        "攻击样例(8 个": ["docs/archive/design.md"],
        # 影子门禁是第 7 项,「六项」docstring 过期(R3-Q1)
        "六项门禁": ["app/graph/gates.py", "app/graph/nodes.py"],
        # compose 冒烟无留档运行,「已实测」不成立(R3-Q1)
        "compose 冒烟已实测": ["docs/adr"],
    }
    for phrase, scopes in forbidden.items():
        for scope in scopes:
            root = REPO_ROOT / scope
            candidates = [root] if root.is_file() else [*root.rglob("*.md"), *root.rglob("*.py")]
            for path in candidates:
                if path.name in {"interview-audit-2026-09-26.md", "PROGRESS.md"}:
                    continue  # 审计报告/归档总日志引用假命题原文或记录当时文案,属记录而非主张
                text = path.read_text(encoding="utf-8")
                assert phrase not in text, f"假命题复活:{path} 仍含 {phrase!r}(P3-7 黑名单)"


def test_p3_7_corrected_claims_are_pinned() -> None:
    """修正后的如实表述必须仍在原文件里(独立于 ADR 锚点节;不钉行号,理由见模块注释)。"""

    def text(relpath: str) -> str:
        return (REPO_ROOT / relpath).read_text(encoding="utf-8")

    # ADR-0001:两处「崩溃恢复」假命题改为 checkpoint 仅留档
    # (M6 改写:恢复接线落地后,现钉"按 superstep 留档 + 位置权威"与
    #  "无可用快照仍由 recover_stale 判死"——文案变了是因为事实变了,不是放松断言)
    adr1 = text("docs/adr/0001-langgraph-编排.md")
    assert "按 superstep 留档" in adr1
    assert "仍由 recover_stale 收敛 NEEDS_REVIEW" in adr1
    # design.md 第三副本(文件已归档到 docs/archive/)
    assert "按 superstep 留档" in text("docs/archive/design.md")
    # 攻击样例 10 个 ×3
    assert "攻击样例(10 个,全部拦截)" in text("docs/archive/design.md")
    assert "攻击样例 10 个拦截" in text("docs/threat-model.md")
    assert "10 个攻击样例" in text("README.md")
    # 七项门禁 docstring(gates.py 模块头 / nodes.py 的 apply 节点)
    assert "七项门禁" in _read("app/graph/gates.py")[0]
    assert "七项门禁" in text("app/graph/nodes.py")
    # ADR-0002:compose 冒烟如实表述
    assert "无留档的实测运行" in text("docs/adr/0002-execution-backend-local-默认.md")
    # 事实基准:checkpoint.py 自述「恢复分两级」仍在(M6:读侧已接线)
    assert "恢复分两级" in text("app/graph/checkpoint.py")


def test_readme_tool_inventory_matches_registry() -> None:
    """README「受控 Agent 工具」那句的条数与名单必须逐名等于 registry 实际注册。

    M3 往目录里加了 find_symbol/describe_file,README 还写着"7 个"——file:line 锚点
    盯不住"条数与名单"这类漂移,所以这里按集合对齐钉死(伪工具 finish 只给模型收敛用,
    不算受控工具,与 README 的口径一致)。
    """
    line = next(line for line in _read("README.md") if "受控 Agent 工具" in line)
    head, _, tail = line.partition("**:")
    declared_count = int(re.search(r"\*\*(\d+) 个", head).group(1))
    names = [name.strip() for name in tail.rstrip(",").split("/")]
    registered = [name for name in REGISTRY if name != FINISH_TOOL]
    assert declared_count == len(names) == len(registered), (
        f"README 声明 {declared_count} 个、名单 {len(names)} 项、registry 注册 {len(registered)} 项"
    )
    assert names == registered, f"README 名单与 registry 不符:{set(names) ^ set(registered)}"


def test_readme_dataset_counts_match_the_fixtures() -> None:
    """README 与 threat-model 的题数/攻击样例数必须以夹具目录为准绳。

    R3-Q1 记的就是这一类漂移(ATTACK-009 入库后「8 个」没回改)。数字写在文案里,
    就得有人每次对着实物数一遍——上一用例管工具清单,这条管数据集。
    """
    attacks = sorted(p.name for p in (REPO_ROOT / "bugs" / "attacks").iterdir() if p.is_dir())
    bugs = sorted(
        p.name for p in (REPO_ROOT / "bugs").iterdir() if p.is_dir() and p.name.startswith("BUG-")
    )
    readme = "\n".join(_read("README.md"))
    threat = "\n".join(_read("docs/threat-model.md"))
    assert f"{len(attacks)} 个攻击样例" in readme, f"README 的攻击样例条数与目录不符:{attacks}"
    assert f"攻击样例 {len(attacks)} 个拦截" in threat, "threat-model 的条数与目录不符"
    assert f"正式集 {len(bugs)} 题" in readme, f"README 的题目数与 bugs/ 不符:{len(bugs)}"


_ROUTE_DECORATOR = re.compile(r"^@router\.(?:get|post|put|patch|delete)\(", re.M)
_FENCE_BLOCK = re.compile(r"```.*?```", re.S)
_MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
_LINK_CHECK_ROOTS = ("README.md", "AGENTS.md")


def test_readme_endpoint_count_matches_routes_py() -> None:
    """README「FastAPI N 个端点」必须以 app/api/routes.py 的路由装饰器条数为准。

    2026-10-12 全库考古查出的漂移:README 写「七个端点」而实际 12 个。工具清单与
    数据集计数早有对应用例,端点数是当时漏网的同一类漂移,这里补上对账口径。
    """
    routes = _ROUTE_DECORATOR.findall("\n".join(_read("app/api/routes.py")))
    readme = "\n".join(_read("README.md"))
    assert f"FastAPI {len(routes)} 个端点" in readme, (
        f"README 的端点数与 app/api/routes.py 不符:实际 {len(routes)} 个"
    )


def test_markdown_links_point_at_existing_files() -> None:
    """根文档与 docs/ 下的每个相对链接都必须指向真实文件。

    归档 4 篇过期文档时发现 `docs/archive/README.md` 有一条 `../x.md` 指错目录:
    锚点用例只管「那句话还在不在」,管不到「链接指没指对」。
    PROGRESS.md(2026-10-16 归档)是按天追加的历史日志,其中链接按当时的仓库根
    目录写——按文件名豁免,不为归档重写历史正文。
    """
    targets = [REPO_ROOT / name for name in _LINK_CHECK_ROOTS]
    targets += sorted((REPO_ROOT / "docs").rglob("*.md"))
    broken: list[str] = []
    for path in targets:
        if path.name == "PROGRESS.md":
            continue  # 归档总日志:链接按当时仓库根目录写,不为归档重写历史正文
        body = _FENCE_BLOCK.sub("", path.read_text(encoding="utf-8"))
        for match in _MD_LINK.finditer(body):
            link = match.group(1)
            if link.startswith(("http", "mailto:", "/", "file:")):
                continue
            if not (path.parent / link).exists():
                broken.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {link}")
    assert not broken, f"链接指向不存在的文件:{'; '.join(broken)}"


def _dep_name(spec: str) -> str:
    """从依赖声明(PEP 508 片段)取包名,如 "pydantic>=2.7" -> "pydantic"。"""
    return re.split(r"[><=!~;\[\s]", spec.strip(), maxsplit=1)[0].strip()


def test_uv_lock_is_tracked_and_in_sync() -> None:
    """依赖声明以 pyproject.toml 为唯一事实源,uv.lock 必须入库且与之同步。

    同一份依赖此前漂过一次:httpx 只写在 requirements-dev.txt,pyproject 的 dev
    组里没有——照 pyproject 装出来的环境跑不起 starlette TestClient 用例
    (2026-10-16 起双入口并入 uv,对账对象从 requirements* 换成 uv.lock)。
    这里先静态对账锁内 patchpilot 条目的运行依赖与 dev 组名单,再用
    `uv lock --check` 整体验证锁没有落后于声明;环境没有 uv 时跳过后半段
    (本地与 CI 均有 uv,由 setup-uv 保证)。
    """
    lock_path = REPO_ROOT / "uv.lock"
    assert lock_path.is_file(), "uv.lock 不存在:依赖以 pyproject.toml 声明、以 uv.lock 锁定"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "uv.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert tracked.returncode == 0, "uv.lock 没有入库:锁文件必须提交,不能只留在本地"

    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    pkg = next(p for p in lock["package"] if p["name"] == "patchpilot")
    cfg = tomllib.loads("\n".join(_read("pyproject.toml")))

    locked_runtime = {d["name"] for d in pkg["dependencies"]}
    declared_runtime = {_dep_name(s) for s in cfg["project"]["dependencies"]}
    assert locked_runtime == declared_runtime, (
        f"uv.lock 的运行依赖与 pyproject 不同:{sorted(locked_runtime ^ declared_runtime)}"
        "(改完依赖先 uv lock 再提交)"
    )
    locked_dev = {d["name"] for d in pkg.get("dev-dependencies", {}).get("dev", [])}
    declared_dev = {_dep_name(s) for s in cfg["dependency-groups"]["dev"]}
    assert locked_dev == declared_dev, (
        f"uv.lock 的 dev 组与 pyproject 不同:{sorted(locked_dev ^ declared_dev)}"
    )

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv 不在 PATH,跳过锁同步校验")
    check = subprocess.run([uv, "lock", "--check"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert check.returncode == 0, (
        f"uv.lock 落后于 pyproject.toml(跑 uv lock 刷新):{check.stdout}{check.stderr}"
    )


def test_interview_site_html_matches_its_markdown(tmp_path: Path) -> None:
    """提交进仓库的站点 HTML 必须与它的 Markdown 事实源逐字节一致。

    `docs/interview-guide/site/index.html` 是 `scripts/build_interview_site.py` 从
    同目录 *.md 生成的,进仓库是为了双击就能读;但它一改 md 就过期,而页面里写着
    "Markdown 为唯一事实源"——这正是 P3-7 那类"文档说的和实际的不是同一份"的复现形态。
    这里把生成物重建到 tmp_path 比对,不动仓库里的文件。
    """
    committed = REPO_ROOT / "docs" / "interview-guide" / "site" / "index.html"
    if not committed.exists():
        pytest.skip("站点 HTML 不在仓库里,无需对账")

    spec = importlib.util.spec_from_file_location(
        "build_interview_site", REPO_ROOT / "scripts" / "build_interview_site.py"
    )
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.OUT = tmp_path / "index.html"
    builder.build()

    assert builder.OUT.read_bytes() == committed.read_bytes(), (
        "docs/interview-guide/site/index.html 与 docs/interview-guide/*.md 不同步:"
        "跑 python scripts/build_interview_site.py 重新生成后一并提交"
    )
