"""P3-7 防复发机制:文档锚点断言(file:line 级)。

背景(interview-audit-2026-09-26.md R3-Q1):夜间 GOAL 模式改代码时周边权威
文档没有同步修订,产出 7 处与代码互斥的假命题,且无任何机制强制一致。
本测试把「文档句」钉在 file:line 锚点上:

1. 每篇 ADR 末尾必须有「验证锚点」节,逐条声明 `路径:行号` — `期望子串`;
2. 锚点所指文件在所指行必须包含期望子串(行号漂移或文案回退都算失败);
3. 已知的假命题文案禁止在任何文档/源码中复活(黑名单);
4. README 的能力清单(工具条数与名单)以代码里的注册表为准绳逐名对齐。

夜间 GOAL 的「全量 pytest 绿」红线由此物理卡住"改代码不改文档"。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.tools.registry import FINISH_TOOL, REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]

# ADR 验证锚点节的条目格式:`- `相对路径:行号` — `期望子串``
_ANCHOR_LINE = re.compile(r"^- `([^`\s]+):(\d+)` — `([^`]+)`$", re.MULTILINE)
_ANCHOR_SECTION = "## 验证锚点"


def _read(relpath: str) -> list[str]:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8").splitlines()


def _iter_anchor_entries() -> list[tuple[str, str, int, str]]:
    """从全部 ADR 的验证锚点节收集 (来源 ADR, 路径, 行号, 期望子串)。"""
    entries: list[tuple[str, str, int, str]] = []
    for adr in sorted((REPO_ROOT / "docs" / "adr").glob("*.md")):
        text = adr.read_text(encoding="utf-8")
        assert _ANCHOR_SECTION in text, f"{adr.name} 缺少「{_ANCHOR_SECTION}」节"
        section = text.split(_ANCHOR_SECTION, 1)[1]
        found = _ANCHOR_LINE.findall(section)
        assert found, f"{adr.name} 的验证锚点节为空(至少声明一条锚点)"
        rel = adr.relative_to(REPO_ROOT).as_posix()
        for path, lineno, expected in found:
            entries.append((rel, path, int(lineno), expected))
    return entries


def test_adr_anchor_sections_exist_with_entries() -> None:
    """每篇 ADR 都有非空的验证锚点节(新 ADR 入库时必须声明锚点)。"""
    adrs = sorted((REPO_ROOT / "docs" / "adr").glob("*.md"))
    assert adrs, "docs/adr/ 下没有任何 ADR"
    entries = _iter_anchor_entries()
    assert len(entries) >= len(adrs), "至少每篇 ADR 一条锚点"


def test_adr_declared_anchors_hold() -> None:
    """ADR 声明的每条锚点:所指文件存在、所指行包含期望子串。"""
    for source, relpath, lineno, expected in _iter_anchor_entries():
        lines = _read(relpath)
        assert len(lines) >= lineno, (
            f"锚点失配:{source} 声明 {relpath}:{lineno},但该文件只有 {len(lines)} 行"
            "(文档改动后必须同步修订锚点)"
        )
        actual = lines[lineno - 1]
        assert expected in actual, (
            f"锚点失配:{source} 声明 {relpath}:{lineno} 应含 {expected!r},实际为 {actual!r}"
        )


def test_p3_7_false_claims_are_gone() -> None:
    """已对账的假命题文案黑名单:不得在文档/源码中复活。"""
    forbidden: dict[str, list[str]] = {
        # 「checkpoint 带来崩溃恢复」与 checkpoint.py:3-5 自述互斥(R3-Q1)
        "免费获得节点级恢复": ["docs/adr", "docs/design.md", "docs", "app", "README.md"],
        "带来崩溃恢复": ["docs/adr", "docs/design.md"],
        # ATTACK-009 新增后「8 个」未回改,实测 9 个(R3-Q1)
        "攻击样例 8 个拦截": ["docs/threat-model.md"],
        "8 个攻击样例": ["README.md"],
        "攻击样例(8 个": ["docs/design.md"],
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
                if path.name == "interview-audit-2026-09-26.md":
                    continue  # 审计报告本身引用假命题原文,属记录而非主张
                text = path.read_text(encoding="utf-8")
                assert phrase not in text, f"假命题复活:{path} 仍含 {phrase!r}(P3-7 黑名单)"


def test_p3_7_corrected_claims_are_pinned() -> None:
    """修正后的如实表述钉在原位(行号级,独立于 ADR 锚点节)。"""
    # ADR-0001:两处「崩溃恢复」假命题改为 checkpoint 仅留档
    # (M6 改写:该表述在恢复接线落地后再次过期,现钉"按 superstep 留档 + 位置权威"与
    #  "无可用快照仍由 recover_stale 判死"——文案变了是因为事实变了,不是放松断言)
    adr1 = _read("docs/adr/0001-langgraph-编排.md")
    assert any("按 superstep 留档" in line for line in adr1[22:26]), adr1[22:26]
    assert any("仍由 recover_stale 收敛 NEEDS_REVIEW" in line for line in adr1[27:32]), adr1[27:32]
    # design.md 第三副本(design.md:62)
    design = _read("docs/design.md")
    assert "按 superstep 留档" in design[61], design[61]
    # 攻击样例 10 个 ×3(design.md:91 / threat-model.md:45 / README.md:20)
    assert "10 个" in design[90], design[90]
    assert "攻击样例 10 个拦截" in _read("docs/threat-model.md")[44]
    assert "10 个攻击样例" in _read("README.md")[19]
    # 七项门禁 docstring(gates.py:1 / nodes.py:apply 节点;行号随 nodes.py 每次插话漂移:
    # 465→463(verify 复查前置)→466(R-2 类型标注加 TYPE_CHECKING 导入)
    # →470(M1 上下文压缩:localize/propose 调用点各插两行)
    # →496(M2 仓库骨架:_persistent_context 方法 + localize/propose 各插 extra_system)
    # →506(M3 检索工具:READ_TOOLS/WRITE_TOOLS 各加 find_symbol/describe_file)
    # →621(M5 PLAN 阶段:plan/route_plan 节点插在 _token_budget_for 与 PROPOSE 之间,
    #        另加 build_plan_prompt 的导入块)
    # →623(M5 复核:plan 也接仓库骨架——计划要能点名文件与符号,只靠单薄的定位结论写不出可执行计划)
    # →661(M6 崩溃恢复:TaskNodes 新增 resume_snapshot 字段与领取方法、prepare 的
    #        ToolContext 构造抽成 _build_ctx 供恢复侧重建,apply 节点整体下移)
    # →657(S02a:TaskNodes 增加 ledger 字段与 __post_init__,其上方导入块同步变化)
    # →655(S02b:时间口径切换 deadline_epoch,propose 节点整体上移)
    assert "七项门禁" in _read("app/graph/gates.py")[0]
    assert "七项门禁" in _read("app/graph/nodes.py")[654]
    # ADR-0002:compose 冒烟如实表述
    adr2 = _read("docs/adr/0002-execution-backend-local-默认.md")
    assert any("无留档的实测运行" in line for line in adr2[28:34]), adr2[28:34]
    # 事实基准:checkpoint.py 自述「恢复分两级」仍在原位(M6:读侧已接线,
    # 此前的「当前没有崩溃恢复路径」随该事实改变而作废,见 ADR-0001 同步修订)
    assert "恢复分两级" in _read("app/graph/checkpoint.py")[2]


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
