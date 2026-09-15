"""评测报告生成:runs 目录 → Markdown 报告(企划书 11.3)。

用法:
    python -m app.evals.report --runs runs/m9 --out docs/eval-report.md
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from app.evals.metrics import annotate, collect_runs, compute_metrics, latest_per_bug

_ATTACK_COUNT = 4  # bugs/attacks/ 中构造的越权样例数(见 tests/test_attacks.py)


def render(runs_root: Path, bugs_root: Path) -> str:
    rows = [annotate(r, bugs_root) for r in collect_runs(runs_root)]
    per_bug = latest_per_bug(rows)
    metrics = compute_metrics(per_bug)
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    providers = ", ".join(metrics["by_category_providers"]) or "n/a"

    lines = [
        "# PatchPilot 评测报告",
        "",
        f"- 生成时间:{generated}",
        f"- 运行目录:`{runs_root}`(共 {len(rows)} 次运行,每题取最新 {len(per_bug)} 题)",
        f"- 模型提供方:**{providers}**",
        "",
        "> **数据来源声明**:本报告由 `python -m app.evals.report` 从运行产物自动生成;",
        "> 每个指标都有判定脚本(metrics.py),无人工标注。当前批次为 fake-replay 回放模型,",
        "> 用于验证平台闭环的确定性,**不代表真实模型成绩**;接入真实模型后同命令重跑即可替换。",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 最终修复率 | {metrics['final_resolution_rate']} |",
        f"| 定位成功率 | {metrics['localization_rate']} |",
        f"| 补丁应用率 | {metrics['patch_application_rate']} |",
        f"| 回归引入率 | {metrics['regression_introduction_rate']} |",
        f"| 越权拦截 | {metrics['security_blocked_count']} 次(另有攻击样例 {_ATTACK_COUNT}/{_ATTACK_COUNT} 被门禁拦截) |",
        f"| 平均修复轮数 | {metrics['avg_rounds']} |",
        f"| 平均耗时 | {metrics['avg_duration_ms']} ms |",
        f"| 平均 Token | {metrics['avg_tokens']} |",
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
            f"{'命中' if row.localized else '未命中'} | {'⚠️ ' + str(len(row.gate_violations)) + ' 项违规' if row.gate_violations else '通过'} |"
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
        "```bash",
        "# 单题回放",
        "python -m app.evals.run_single --bug BUG-001 --model fake --engine graph --out runs",
        "# 批量评测 + 本报告",
        "python -m app.evals.report --runs runs/m9 --out docs/eval-report.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成评测报告")
    parser.add_argument("--runs", default="runs/m9")
    parser.add_argument("--bugs", default="bugs")
    parser.add_argument("--out", default="docs/eval-report.md")
    args = parser.parse_args(argv)

    report = render(Path(args.runs), Path(args.bugs))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8", newline="\n")
    print(f"[report] {out} ({len(report.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    main()
