# 工业使用改进执行进度

任务定义：[改进计划](industrialization-plan-2026-10-15.md)。
状态只在此处维护；旧 Agent 升级进度继续保存原 G 卡，不在两处复制维护 P 卡。

## D00 — 计划与交接文档（2026-10-15）

- 用户请求：根据本轮审核生成完整改进计划，交其他 Agent 执行。
- 开始 HEAD：`972a1e8cc1fd4742207d3645339ed5682faaf680`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 允许范围：本目录的 industrialization-plan/progress/handoff 三份日期文档、
  `docs/work-notes/README.md`、`docs/README.md`、`PROGRESS.md`。
- 工作：将审核风险映射为 P00–P21，声明生产目标、范围、依赖、测试种子、验收矩阵、授权边界和交接纪律。
- 本卡不实施产品改造、不启动外部任务、不付费、不 push、不发布。
- 验证：`.validation/20261015-225844-f69a6a00.json`，六份文档 diff 检查与现有
  docs_anchors 文件的 10 项检查通过；执行步骤约 1.3 秒。最初临时目录权限失败的
  `.validation/20261015-225833-7d36743d.json` 为 incomplete，保留且不算通过。
- 未运行：产品回归、真实 Docker/负载/模型实验、CI；本卡不复跑上轮 44 项产品测试。
  最后仅登记本段收据，不为记账重跑检查；实现卡与最终版本完整回归交 CI。
- Commit：本段所在 `docs(plan)` 提交；不为写自身 hash 再提交。

## 任务状态

| 卡 | 状态 | 依赖 | owner / commit / 收据 |
|---|---|---|---|
| P00 | TODO | 无 | — |
| P01 | TODO | P00 | — |
| P02 | TODO | P01 | — |
| P03 | TODO | P02 | — |
| P04 | TODO | P00/P02 | — |
| P05 | TODO | P00/P04 | — |
| P06 | TODO | P03/P05 | — |
| P07 | TODO | P00 | — |
| P08 | TODO | P04/P07 | — |
| P09 | TODO | P07/P08 | — |
| P10 | TODO | P01/P03/P04 | — |
| P11 | TODO | P01/P03/P04/P09/P10 | — |
| P12 | TODO | P11 | — |
| P13 | TODO | P04/P11/P12 | — |
| P14 | TODO | P01/P03/P06/P11/P12 | — |
| P15 | TODO | P00 | — |
| P16 | TODO | P03/P06/P11/P12/P15 | — |
| P17 | TODO | P00/P06/P08/P09 | — |
| P18 | TODO | P15/P16/P17 | — |
| P19 | TODO | P05–P15/P17 | — |
| P20 | TODO | P00–P19 | — |
| P21 | OPTIONAL_NOT_STARTED | M3/新规格 | — |

状态建议：TODO / IN_PROGRESS / IMPLEMENTED_PENDING_EVIDENCE / NOT_RUN_ENV /
READY_FOR_AUTHORIZATION / DONE。没有真实环境证据时不要把关键卡直接记 DONE。

## 后续每卡记录模板

```text
卡号/子卡/owner/开始与结束源码身份：
改动范围与已有 dirty：
具体问题、实现、为何必要：
规范与语义差异/兼容/迁移/回滚：
验收输入→动作→观测结果；证据等级与工件位置/hash：
verify 命令、退出码、耗时、收据；环境失败/skip/未运行分开：
commit；未验证项与 CI/外部证据依赖：
费用/外部动作；下一卡：
```
