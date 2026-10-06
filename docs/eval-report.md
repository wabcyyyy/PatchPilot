# PatchPilot 评测报告

- 生成时间:2026-10-06 07:21 UTC
- 运行目录:`runs\fake35-2026-10-06`(共 35 次运行,每题取最新 35 题)
- 模型提供方:**fake-replay**
- 引擎:plain
- 批次溯源:执行后端 local · 代码 8ab4e0813673(取自批次最新运行)

> **数据来源声明**:本报告由 `python -m app.evals.report` 从运行产物自动生成;
> 每个指标都有判定脚本(metrics.py),无人工标注。当前批次为 fake-replay 回放模型,用于验证平台闭环的确定性,**不代表真实模型成绩**;接入真实模型后同命令重跑即可替换。

## 汇总指标

| 指标 | 值 |
|---|---|
| 最终修复率(每题取最新) | 1.0 |
| 全量运行修复率(含同题重试,35 次) | 1.0 |
| 定位成功率 | 1.0 |
| 补丁应用率 | 1.0 |
| 回归引入率 | 0.0 |
| 越权拦截 | 0 次(攻击样例 10 个,拦截验证见 tests/test_attacks.py) |
| 平均修复轮数 | 1.0 |
| 平均耗时 | 7816 ms |
| 平均 Token | 177 |
| 耗时 min/p50/p95/max | 6770 / 7589 / 9667 / 10053 ms |
| Token min/max | 133 / 314 |
| 轮数分布(1 / 2 / 3+) | 35 / 0 / 0 |
| 判型计数 | resolved 35 · 门禁拦截 0 · needs_review 0 · 其他 0 |

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
| BUG-029 | ✅ resolved | FINISHED | 1 | `src/aggregator.py` | 命中 | 通过 |
| BUG-030 | ✅ resolved | FINISHED | 1 | `src/checkout.py`, `src/fees.py` | 命中 | 通过 |
| BUG-031 | ✅ resolved | FINISHED | 1 | `src/dec.py` | 命中 | 通过 |
| BUG-032 | ✅ resolved | FINISHED | 1 | `src/units.py` | 命中 | 通过 |
| BUG-033 | ✅ resolved | FINISHED | 1 | `src/notify.py`, `src/rules.py` | 命中 | 通过 |
| BUG-034 | ✅ resolved | FINISHED | 1 | `src/escaping.py`, `src/exporter.py` | 命中 | 通过 |
| BUG-035 | ✅ resolved | FINISHED | 1 | `src/escapes.py`, `src/template.py` | 命中 | 通过 |

## 失败任务复盘索引

(本批次无失败任务)

## 复现方式

以下命令由本批次运行产物的 provenance 字段归并生成(模型/引擎/目录均为批次实际取值):

```bash
# 单题示例(--bug 换成同批任意题目即可)
python -m app.evals.run_single --bug BUG-001 --model fake --engine plain --out runs/fake35-2026-10-06
python -m app.evals.report --runs runs/fake35-2026-10-06 --out docs/eval-report.md
```
