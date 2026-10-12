# PatchPilot 面试学习资料（interview-guide）

对 `D:\wcy\project\PatchPilot` 做的一次**逆向考古**产物：面向 AI Agent / AI 应用 / AI 全栈岗位，3 天准备期。
**Markdown 是站点内容源；产品事实以当前源码、对应测试与有效 SPEC/ADR 共同核对**；HTML 只负责阅读体验（`site/index.html`，无外部依赖）。

## 校准范围与事实源

它不描述设计意图，它描述**当前代码能被证明的事实**，并把文档与代码的偏差单列成表。事实优先级：
当前执行代码与测试、有效 SPEC/ADR 共同核对；历史记录只证明对应时间/身份下的行为，不当作当前能力。

标注体系（所有文件统一）：`代码事实` / `合理推断` / `实际测试` / `面试知识` / `生产化扩展` / `风险点` / `面试口径`。

## 阅读顺序

| 序号 | 文件 | 什么时候读 |
|---|---|---|
| 00 | [项目考古报告](00-project-audit.md) | **先读**：地图、真实执行链、冲突表、风险清单 |
| — | [docs 清理报告](doc-cleanup-report.md) | 本轮修改与已有历史工作区改动的区别 |
| 01 | [项目介绍三版本](01-project-overview.md) | Day 1：30 秒 / 90 秒 / 3 分钟 |
| 02 | [架构逐模块](02-architecture.md) | Day 1–2：当前模块、调用关系与取舍 |
| 03 | [风险登记表](03-risk-register.md) | 当前风险与证据边界 |
| 04 | [核心代码精读](04-code-walkthrough.md) | Day 1：验收/身份/恢复/账本代码 + Python 语法点 |
| 05 | [怎么证明有效](05-evaluation.md) | Day 2：能证明 / 不能证明 / 实验设计 |
| 06 | [Demo → Production 缺口](06-production-gap.md) | Day 3：未来扩展，不作为现有能力 |
| 07 | [AI 应用知识地图](07-ai-knowledge-map.md) | Day 2：P0 26 条 + P1/P2 |
| 08 | [项目专属面试题](08-interview-questions.md) | Day 2–3：20 题 × 3–5 层追问链 |
| 09 | [面试表达稿](09-interview-script.md) | Day 3：亮点/不足措辞、没上线怎么答、AI Coding |
| 10 | [简历表述体检](10-claim-check.md) | Day 3：能写 / 需弱化 / 不建议写 |
| 11 | [速记卡](11-cheatsheet.md) | **面试前 30 分钟** |
| 12 | [3 天排程](12-3day-plan.md) | 开工前定计划（必读/必会/可跳过） |
| 13 | [三轮模拟面试](13-mock-interview.md) | Day 3 实战（第三轮建议录音） |

## HTML 站

```bash
python scripts/build_interview_site.py     # 重新生成 docs/interview-guide/site/index.html
```
浅色主题、单文件、无网络依赖、直接双击打开：左侧导航（滚动时高亮当前篇）+ 全文搜索（命中即自动展开该篇里含关键词的追问/答案卡）+ P0/P1/P2 与标签着色 + 打印时隐藏导航。窄屏用"目录"按钮开合侧栏。

## 三条纪律（贯穿全部材料）

1. 不把生产化扩展说成已实现；
2. 不维护当前通过数/代码规模快照；历史实验数字带提交、环境、样本和局限；
3. 项目真实性优先于包装 —— 这份材料的全部价值在于"我知道哪里弱"。

本轮校准见 [任务卡与收据](../work-notes/realignment-2026-10-12.md)。未发起付费实验或本地全量。
