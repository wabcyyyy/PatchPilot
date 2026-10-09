"""评测报告生成:runs 目录 → Markdown 报告(企划书 11.3)。

用法:
    python -m app.evals.report --runs runs/m9 --out docs/eval-report.md
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.evals.metrics import RunRow, annotate, collect_runs, compute_metrics, latest_per_bug


def _attack_count(bugs_root: Path) -> int:
    """bugs/attacks/ 下的攻击样例数(自动发现,增删样例不再漂移)。"""
    attacks = Path(bugs_root) / "attacks"
    if not attacks.is_dir():
        return 0
    return sum(1 for p in attacks.iterdir() if p.is_dir() and p.name.startswith("ATTACK-"))


def _model_flag(provider: str) -> str:
    """report.json 里的 provider → run_single 的 --model 取值(fake-replay → fake)。"""
    return "openai" if provider == "openai" else "fake"


def repro_commands(runs_root: Path, per_bug: list[RunRow], report_out: str) -> list[str]:
    """从批次行归并出复现命令(T10.1):provider/engine/arm 取自运行产物,不再手写。

    同一批次可能出现多种 (model, engine, arm) 组合,每种给一行单题示例。
    """
    combos: dict[tuple[str, str, str], str] = {}
    for row in sorted(per_bug, key=lambda r: r.bug_id):
        provider = str(row.provenance.get("model_provider") or row.model_provider)
        # P3-16:兜底 "unknown" 而非 "graph"——146 份历史 report 无一同时缺
        # engine 与 provenance(实测),但真缺时编造可执行的假命令比承认
        # "引擎不明"更糟:「不可执行的诚实」优于「可执行的错误」
        engine = str(row.provenance.get("engine") or row.engine or "unknown")
        # 执行体(消融臂)必须进命令:同一 engine 下两臂跑出的结果不可互复现,
        # 漏了 --arm 就等于给对照批生成一条会跑出"默认臂"的假复现命令
        arm = str(row.provenance.get("arm") or "agent")
        combos.setdefault((_model_flag(provider), engine, arm), row.bug_id)

    lines = []
    if not combos:  # 空批次:退化为通用示例(plain 引擎,驱动器真实支持)
        combos = {("fake", "plain", "agent"): "BUG-001"}
    for (model_flag, engine, arm), bug_id in sorted(combos.items()):
        arm_flag = "" if arm == "agent" else f" --arm {arm}"
        lines.append(
            f"python -m app.evals.run_single --bug {bug_id}"
            f" --model {model_flag} --engine {engine}{arm_flag} --out {runs_root.as_posix()}"
        )
    lines.append(f"python -m app.evals.report --runs {runs_root.as_posix()} --out {report_out}")
    return lines


def _provenance_note(per_bug: list[RunRow]) -> str:
    """从批次最新一次运行的 provenance 归并出溯源说明;无 provenance 返回空串。"""
    if not per_bug:
        return ""
    prov = max(per_bug, key=lambda r: r.finished_stamp).provenance
    if not prov:
        return ""
    parts = []
    if prov.get("model_name"):
        parts.append(f"模型 {prov['model_name']}")
    if prov.get("execution_backend"):
        parts.append(f"执行后端 {prov['execution_backend']}")
    commit = str(prov.get("git_commit") or "")
    if commit:
        parts.append(f"代码 {commit[:12]}")
    return " · ".join(parts)


def _blind_flag(runs_root: Path) -> bool:
    """批次是否为盲跑对照(E6):读 batch_manifest.json 的 blind 标记;
    无 manifest(历史批次/单任务目录)视为非盲跑。"""
    try:
        manifest = json.loads((Path(runs_root) / "batch_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(manifest.get("blind"))


def _pctl(values: list[int], q: float) -> int | None:
    """最近秩分位数(报告层口径,非统计库);空输入返回 None → 展示 n/a。"""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _fmt(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def distribution_rows(per_bug: list[RunRow]) -> list[tuple[str, str]]:
    """批次分布与判型计数(E7):让"太软的评测集"在报告上自我暴露。

    数据全部来自 report.json 现有字段(duration/tokens/rounds/verdict),
    不重跑任务,metrics.py 判定逻辑零改动;空批次/缺字段记 n/a 不抛异常。

    判型口径(P3-13):resolved/needs_review 统一按 verdict 计;
    门禁拦截按 gate_violations 计(跨引擎通用)——PATCH_REJECTED 对 graph
    永不为终态(轮尽回滚后落 BUDGET_EXCEEDED),按 status 计数会把
    graph 批的门禁拦截系统性记 0。四类互斥,合计恒等于任务数。
    """
    if not per_bug:
        return [
            ("耗时 min/p50/p95/max", "n/a"),
            ("Token min/max", "n/a"),
            ("轮数分布(1 / 2 / 3+)", "n/a"),
            ("判型计数", "n/a"),
        ]
    durations = [r.duration_ms for r in per_bug]
    tokens = [r.tokens_used for r in per_bug]
    rounds = {"1": 0, "2": 0, "3+": 0}
    for row in per_bug:
        key = "1" if row.rounds <= 1 else "2" if row.rounds == 2 else "3+"
        rounds[key] += 1
    counts = {"resolved": 0, "门禁拦截": 0, "needs_review": 0, "其他": 0}
    for row in per_bug:
        if row.verdict == "resolved":
            counts["resolved"] += 1
        elif row.verdict == "needs_review":
            counts["needs_review"] += 1
        elif row.gate_violations:
            counts["门禁拦截"] += 1
        else:
            counts["其他"] += 1
    counter_text = " · ".join(f"{key} {value}" for key, value in counts.items())
    return [
        (
            "耗时 min/p50/p95/max",
            f"{_fmt(min(durations))} / {_fmt(_pctl(durations, 0.50))} / "
            f"{_fmt(_pctl(durations, 0.95))} / {_fmt(max(durations))} ms",
        ),
        ("Token min/max", f"{_fmt(min(tokens))} / {_fmt(max(tokens))}"),
        ("轮数分布(1 / 2 / 3+)", f"{rounds['1']} / {rounds['2']} / {rounds['3+']}"),
        ("判型计数", counter_text),
    ]


def render(runs_root: Path, bugs_root: Path, report_out: str = "docs/eval-report.md") -> str:
    rows = [annotate(r, bugs_root) for r in collect_runs(runs_root)]
    per_bug = latest_per_bug(rows)
    metrics = compute_metrics(per_bug)
    # S09:汇总明确携带指标版本与 strict 语义边界——不允许新旧口径混排成无版本数字
    generated = (
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"metrics_version={metrics.get('metrics_version')}"
        "(localized_strict=未触碰参考范围外文件且非空;expected_coverage 仅对有 "
        "reference.diff 的样本,分母=|expected|)"
    )
    providers = ", ".join(metrics["by_category_providers"]) or "n/a"
    # 只有明确是真实提供方的批次才允许宣称"真实模型成绩";
    # fake/unknown(模型对象缺 provider 属性)一律按回放口径声明,不产出漂亮假数字
    if providers == "n/a" or "fake" in providers or "unknown" in providers:
        batch_note = (
            "当前批次为 fake-replay 回放模型,用于验证平台闭环的确定性,"
            "**不代表真实模型成绩**;接入真实模型后同命令重跑即可替换。"
        )
    else:
        batch_note = (
            f"本批次为**真实模型在线调用成绩**({providers}),"
            "结论由平台测试执行与门禁脚本自动判定;"
            "各任务 report.json 内含 token 明细与成本(约值,价目表未收录的模型显示 n/a)。"
        )

    engines = ", ".join(sorted({r.engine for r in per_bug})) or "n/a"
    # P1-3 整改:披露读取口径——"每题取最新"会掩盖同题重试,双口径并排呈现
    all_runs_resolved = sum(1 for r in rows if r.verdict == "resolved")
    all_runs_rate = round(all_runs_resolved / len(rows), 4) if rows else None
    lines = [
        "# PatchPilot 评测报告",
        "",
        f"- 生成时间:{generated}",
        f"- 运行目录:`{runs_root}`(共 {len(rows)} 次运行,每题取最新 {len(per_bug)} 题)",
        f"- 模型提供方:**{providers}**",
        f"- 引擎:{engines}",
    ]
    provenance_note = _provenance_note(per_bug)
    if provenance_note:
        lines.append(f"- 批次溯源:{provenance_note}(取自批次最新运行)")
    if _blind_flag(runs_root):
        lines.append(
            "- **盲跑对照批次**:装载时不带 issue 描述"
            "(占位替换,失败/回归测试集原样保留)——"
            "度量 localize 的真实贡献,与正常批次对比阅读"
        )
    lines += [
        "",
        "> **数据来源声明**:本报告由 `python -m app.evals.report` 从运行产物自动生成;",
        f"> 每个指标都有判定脚本(metrics.py),无人工标注。{batch_note}",
        "> **口径说明**:定位成功=弱信号(命中任一期望文件即计,严格口径=触碰集⊆期望集,双行并排);"
        "回归判定基于导入时抽样的 p2p 回归子集,不覆盖全量回归测试。",
        "",
    ]
    # P3-18:软集形态警报——真实模型批全绿且全部 1 轮时,该形态本身就是
    # "评测集可能太软"的证据(fake 回放全 1 轮是脚本构造使然,不触发)。
    # 历史 28 题真实模型记录即为此形态,却曾被夜报当通过证据引用,无任何警报。
    real_rows = [r for r in per_bug if r.model_provider not in ("fake-replay", "unknown", "")]
    if (
        len(real_rows) >= 5
        and all(r.verdict == "resolved" for r in real_rows)
        and all(r.rounds <= 1 for r in real_rows)
    ):
        lines.append(
            f"> ⚠️ **软集形态警报(P3-18)**:本批次 {len(real_rows)} 个真实模型任务"
            "全部 resolved 且全部 1 轮通过——评测集区分度存疑,"
            "请勿把该形态单独当效果证据引用;建议引入 hard 题或盲跑对照批复核。"
        )
        lines.append("")
    lines += [
        "## 汇总指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 最终修复率(每题取最新) | {metrics['final_resolution_rate']} |",
        f"| 全量运行修复率(含同题重试,{len(rows)} 次) | {all_runs_rate} |",
        f"| 定位成功率(命中任一期望文件) | {metrics['localization_rate']} |",
        f"| 定位成功率(严格,触碰集⊆期望集) | {metrics['localization_strict_rate']} |",
        f"| 补丁应用率 | {metrics['patch_application_rate']} |",
        f"| 回归引入率 | {metrics['regression_introduction_rate']} |",
        f"| 越权拦截 | {metrics['security_blocked_count']} 次(攻击样例 {_attack_count(bugs_root)} 个,拦截验证见 tests/test_attacks.py) |",
        f"| 平均修复轮数 | {metrics['avg_rounds']} |",
        f"| 平均耗时 | {metrics['avg_duration_ms']} ms |",
        f"| 平均 Token | {metrics['avg_tokens']} |",
        # E7:分布与判型计数——均值与通过率之外,让重试/失败/耗时离群在报告上可见
        *(f"| {label} | {value} |" for label, value in distribution_rows(per_bug)),
        "",
        "## 分题结果",
        "",
        "| 题目 | 结论 | 状态 | 轮数 | 变更文件 | 定位 | 门禁 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in sorted(per_bug, key=lambda r: r.bug_id):
        mark = {"resolved": "✅", "needs_review": "👤"}.get(row.verdict, "❌")
        lines.append(
            f"| {row.bug_id} | {mark} {row.verdict} | {row.status} | {row.rounds} | "
            f"{', '.join(f'`{f}`' for f in row.changed_files) or '—'} | "
            f"{'严格命中' if row.localized_strict else '命中' if row.localized else '未命中'} | "
            f"{'⚠️ ' + str(len(row.gate_violations)) + ' 项违规' if row.gate_violations else '通过'} |"
        )

    failures = [r for r in per_bug if r.verdict != "resolved"]
    lines += ["", "## 失败任务复盘索引", ""]
    if failures:
        for row in failures:
            lines.append(
                f"- {row.bug_id}({row.status}):{row.error or '见轨迹'} → `{row.run_dir.name}`"
            )
        lines += ["", "详细复盘见 `docs/postmortems/`。"]
    else:
        lines.append("(本批次无失败任务)")

    lines += [
        "",
        "## 复现方式",
        "",
        "以下命令由本批次运行产物的 provenance 字段归并生成(模型/引擎/目录均为批次实际取值):",
        "",
        "```bash",
        "# 单题示例(--bug 换成同批任意题目即可)",
        *repro_commands(runs_root, per_bug, report_out),
        "```",
        "",
    ]
    return "\n".join(lines)


def render_multi(
    runs_roots: list[Path], bugs_root: Path, report_out: str = "docs/eval-report.md"
) -> str:
    """多批次对比报告(T10.4):每个批次独立分节,含各自的指标/溯源/复现口径。

    批次间用水平线分隔;指标口径完全一致(同一 metrics.py 判定),便于横向对比。
    """
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    roots = ", ".join(f"`{root.as_posix()}`" for root in runs_roots)
    parts = [
        "# PatchPilot 评测报告(多批次对比)",
        "",
        f"- 生成时间:{generated}",
        f"- 参与对比的批次({len(runs_roots)} 个):{roots}",
        "- 各批次分节展示;指标判定口径一致(metrics.py),可直接横向对比。",
    ]
    for root in runs_roots:
        parts += ["", "---", ""]
        parts.append(render(root, bugs_root, report_out=report_out))
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成评测报告")
    parser.add_argument(
        "--runs",
        action="append",
        default=None,
        help="runs 目录;可多次传入做批次对比(默认 runs/m9)",
    )
    parser.add_argument("--bugs", default="bugs")
    parser.add_argument("--out", default="docs/eval-report.md")
    args = parser.parse_args(argv)

    runs_roots = [Path(r) for r in args.runs] if args.runs else [Path("runs/m9")]
    if len(runs_roots) == 1:
        report = render(runs_roots[0], Path(args.bugs), report_out=args.out)
    else:
        report = render_multi(runs_roots, Path(args.bugs), report_out=args.out)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8", newline="\n")
    print(f"[report] {out} ({len(report.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    main()
