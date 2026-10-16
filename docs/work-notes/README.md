# 阶段材料索引

这里保留当前升级交接、审查、报告快照和探针。已执行或已被替代的计划及收口记录见 [历史计划索引](../archive/README.md)，长期设计见 [文档入口](../README.md)。

## 当前升级工作

工业使用改进从 [2026-10-15 完整计划](industrialization-plan-2026-10-15.md) 进入；
执行进度见 [工业化执行进度](industrialization-progress-2026-10-15.md)；
交接提示见 [Agent 交接提示](industrialization-handoff-2026-10-15.md)；
正式收尾报告见 [工业化收尾任务卡](closeout-industrialization-2026-10-16.md)；
企业级多实例扩展见 [P21 多实例多租户规格](p21-multi-instance-spec.md)。全量任务卡（P00 ~ P20）已完成实施与演练验收，正式完结收口。

| 文件 | 用途 |
|---|---|
| [新对话升级交接](PatchPilot升级交接Prompt-2026-10-10.md) | 在 D 盘实际项目从当前卡/批次接续 |
| [升级执行进度](agent-upgrade-progress-2026-10-10.md) | 卡状态、提交、测试收据与剩余限制的唯一进度源 |
| [SPEC 复核记录](agent-upgrade-spec-review-2026-10-11.md) | 2026-10-11 规格自检、文档修正与跨卡差距 |
| [Agent 落地评审](agent-readiness-review-2026-10-10.md) | 本轮审查依据；历史源码基线须重新核验 |
| [离线探针脚本](audit_agent_review_20261010.py) | Mock 请求与历史运行汇总 |
| [探针结果](2026-10-10-agent-review-probes.json) | 原始审查结果，未重算 |

任务定义见 [升级 SPEC](../PatchPilot修复Agent升级SPEC-2026-10-10.md)，当前状态与下一批见执行进度。文档中的初始审查、B0 背景和历史测试结果不替代当前执行收据。

## 审查与参考材料

| 文件 | 用途 |
|---|---|
| [2026-09-19 自检](../archive/audit-2026-09-19.md) | 当时风险清单 |
| [2026-09-20 LOOP 审查](../archive/audit-2026-09-20.md) | 当时审查与整改过程 |
| [2026-09-24 面试审查](interview-audit-2026-09-24.md) | 第一轮对抗审查 |
| [2026-09-26 面试审查](interview-audit-2026-09-26.md) | 第二轮对抗审查 |
| [2026-09-26 整改对照](audit-2026-09-26-remediation.md) | 问题与整改记录 |
| [E3 与 provenance 补证](../archive/e3-and-provenance-evidence-2026-09-26.md) | 连通性与历史来源核验 |
| [SWE 批次与消融记录](swe-ablation-evidence-2026-10-07.md) | 当时题单、负结果和瓶颈 |
| [简历证据](resume-evidence-2026-10-08.md) | 可用主张与证明上限 |
| [面试证据清单](interview-evidence-2026-10-07.md) | 表述与追问材料 |
| [Fake 评测快照](eval-report.md) | 回放结果与文首口径说明 |
| [真实模型历史快照](eval-report-real.md) | 历史结果及其限制 |

这些材料反映各自版本，不直接证明当前源码行为。原始测量仍在 `../evidence/`；已有日志中的旧路径保留当时身份。

## 新增材料

当前交接、进度、审查与报告放本目录。计划执行后，连同完成报告移到 `../archive/` 并记录归档状态；长期规范和当前主规格留在 docs 顶层。
