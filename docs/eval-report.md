# PatchPilot 评测报告

- 生成时间:2026-09-17 23:28 UTC
- 运行目录:`runs\m9`(共 50 次运行,每题取最新 28 题)
- 模型提供方:**fake-replay**

> **数据来源声明**:本报告由 `python -m app.evals.report` 从运行产物自动生成;
> 每个指标都有判定脚本(metrics.py),无人工标注。当前批次为 fake-replay 回放模型,
> 用于验证平台闭环的确定性,**不代表真实模型成绩**;接入真实模型后同命令重跑即可替换。

## 汇总指标

| 指标 | 值 |
|---|---|
| 最终修复率 | 1.0 |
| 定位成功率 | 1.0 |
| 补丁应用率 | 1.0 |
| 回归引入率 | 0.0 |
| 越权拦截 | 0 次(另有攻击样例 4/4 被门禁拦截) |
| 平均修复轮数 | 1.0 |
| 平均耗时 | 6664 ms |
| 平均 Token | 163 |

## 分题结果

| 题目 | 结论 | 状态 | 轮数 | 变更文件 | 定位 | 门禁 |
|---|---|---|---|---|---|---|
| BUG-001 | ✅ resolved | FINISHED | 1 | `src/dateparse.py` | 命中 | 通过 |
| BUG-002 | ✅ resolved | FINISHED | 1 | `src/pagination.py` | 命中 | 通过 |
| BUG-003 | ✅ resolved | FINISHED | 1 | `src/labels.py` | 命中 | 通过 |
| BUG-004 | ✅ resolved | FINISHED | 1 | `src/config.py` | 命中 | 通过 |
| BUG-005 | ✅ resolved | FINISHED | 1 | `src/helpers.py` | 命中 | 通过 |
| BUG-006 | ✅ resolved | FINISHED | 1 | `src/math_ops.py` | 命中 | 通过 |
| BUG-007 | ✅ resolved | FINISHED | 1 | `src/seq.py` | 命中 | 通过 |
| BUG-008 | ✅ resolved | FINISHED | 1 | `src/text_stats.py` | 命中 | 通过 |
| BUG-009 | ✅ resolved | FINISHED | 1 | `src/dictops.py` | 命中 | 通过 |
| BUG-010 | ✅ resolved | FINISHED | 1 | `src/units.py` | 命中 | 通过 |
| BUG-011 | ✅ resolved | FINISHED | 1 | `src/strconv.py` | 命中 | 通过 |
| BUG-012 | ✅ resolved | FINISHED | 1 | `src/collections_ops.py` | 命中 | 通过 |
| BUG-013 | ✅ resolved | FINISHED | 1 | `src/caching.py` | 命中 | 通过 |
| BUG-014 | ✅ resolved | FINISHED | 1 | `src/slicing.py` | 命中 | 通过 |
| BUG-015 | ✅ resolved | FINISHED | 1 | `src/fileio.py` | 命中 | 通过 |
| BUG-016 | ✅ resolved | FINISHED | 1 | `src/tabular.py` | 命中 | 通过 |
| BUG-017 | ✅ resolved | FINISHED | 1 | `src/stats.py` | 命中 | 通过 |
| BUG-018 | ✅ resolved | FINISHED | 1 | `src/calendar_ops.py` | 命中 | 通过 |
| BUG-019 | ✅ resolved | FINISHED | 1 | `src/seq.py` | 命中 | 通过 |
| BUG-020 | ✅ resolved | FINISHED | 1 | `src/pricing.py` | 命中 | 通过 |
| BUG-021 | ✅ resolved | FINISHED | 1 | `src/notes.py` | 命中 | 通过 |
| BUG-022 | ✅ resolved | FINISHED | 1 | `src/carts.py` | 命中 | 通过 |
| BUG-023 | ✅ resolved | FINISHED | 1 | `src/schedule.py` | 命中 | 通过 |
| BUG-024 | ✅ resolved | FINISHED | 1 | `src/render.py` | 命中 | 通过 |
| BUG-025 | ✅ resolved | FINISHED | 1 | `src/stats.py` | 命中 | 通过 |
| BUG-026 | ✅ resolved | FINISHED | 1 | `src/dispatch.py` | 命中 | 通过 |
| BUG-027 | ✅ resolved | FINISHED | 1 | `src/dedup.py` | 命中 | 通过 |
| BUG-028 | ✅ resolved | FINISHED | 1 | `src/accounting.py` | 命中 | 通过 |

## 失败任务复盘索引

(本批次无失败任务)

## 复现方式

```bash
# 单题回放
python -m app.evals.run_single --bug BUG-001 --model fake --engine graph --out runs
# 批量评测 + 本报告
python -m app.evals.report --runs runs/m9 --out docs/eval-report.md
```
