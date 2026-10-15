# 文档入口

docs 顶层保留长期规范、设计与当前升级规格。当前交接、审查、评测快照和探针集中在 [work-notes/](work-notes/README.md)。已执行或被替代的计划与收口记录集中在 [archive/](archive/README.md)。

## 当前最常用的入口

工业使用目标、任务依赖、验收与执行范围见
[2026-10-15 工业使用改进计划](work-notes/industrialization-plan-2026-10-15.md)；
执行进度与各卡收据见 [工业化执行进度](work-notes/industrialization-progress-2026-10-15.md)；
运维值守与应急预案见 [运维值守手册](runbook.md)；
受控生产试点准入证据见 [受控生产试点发布证据](pilot-release-evidence.md)。

| 要做什么 | 文档 | 使用边界 |
|---|---|---|
| 核对修复 Agent 升级范围 | [修复 Agent 升级 SPEC](PatchPilot修复Agent升级SPEC-2026-10-10.md) | B6/G16离线批已收口；G15可选、G17在线批未启动，状态与收据见执行进度 |
| 让新对话接续升级 | [升级交接提示](work-notes/PatchPilot升级交接Prompt-2026-10-10.md) | 从当前进度继续；B0 模板仅供初始启动 |
| 理解本次升级的依据 | [2026-10-10 Agent 评审](work-notes/agent-readiness-review-2026-10-10.md) | 对应审查时的源码和历史运行，不能代替后续验证 |

## 规范与历史设计参考

| 文档 | 回答的问题 |
|---|---|
| [项目企划书](PatchPilot项目企划书.md) | 项目范围、架构、状态机、门禁和评测定义 |
| [历史设计说明](archive/design.md) | 当时设计取舍、接线现状和已知边界；当前行为按源码与 SPEC 核对 |
| [威胁模型](threat-model.md) | 信任边界、已防护及未覆盖的风险 |
| [学习计划](archive/PatchPilot学习计划.md) | 学习路线与自查标准 |
| [历史 Docker 后端说明](archive/docker-backend-notes.md) | 当时执行后端接线和环境要求 |
| [历史 Docker 隔离记录](archive/docker-isolation-notes.md) | 当时容器隔离实验及其边界 |
| [历史实验预登记 v2](archive/experiment-preregistration-v2-2026-10-08.md) | 当时实验设计；文档存在不构成付费授权 |

当前产品行为以源码、对应版本的规范/ADR 和可复核证据共同确认。新升级 SPEC 描述目标行为；旧审查和计划中的“待修/完成”都是当时状态，不能直接作为当前结论。

当前执行链与已确认漂移见 [2026-10-12 实现校准](work-notes/realignment-2026-10-12.md)。
面试材料从 [interview-guide](interview-guide/README.md) 进入。
当前验收/恢复按 ADR-0009/0010；回执/记忆按 ADR-0012/0013；容量/profile 与仓库说明按
ADR-0014/0015；Agent 升级范围按当前升级 SPEC。企划书保留范围与概要，原阶段规划不作为当前完成证据。

## 目录分工

```text
docs/
├── README.md                       文档入口
├── PatchPilot修复Agent升级SPEC-…    当前升级规格
├── 项目企划/学习计划等            长期维护文档
├── adr/                            架构决策及验证锚点
├── postmortems/                    单个失败案例的复盘
├── evidence/                       原始测量、日志和验收产物
├── work-notes/                     阶段审查、临时任务、交接、报告和探针
└── archive/                        已执行计划、历史清单与收口记录
```

- [阶段材料索引](work-notes/README.md)：当前交接、审查、报告快照和参考证据。
- [历史计划索引](archive/README.md)：已完成计划、停手批次、已替代清单及对应执行记录。
- `adr/`：保留现有决策编号；验证锚点格式为 `` `路径` — `期望子串` ``，**不写行号**（行号写法由 `tests/test_docs_anchors.py` 拒绝）。
- `postmortems/`：保留 PM 编号，按失败主题查找。
- `evidence/`：原始产物保留生成身份、分母和失败结果，不手改数字、不重生成历史批次。
- `archive/`：完成、停手或被替代的计划放入历史目录；原始正文保留，索引注明完成范围与未执行项。

## 维护约定

1. 新增阶段审查、进度、交接、临时 SPEC 和报告放在 `docs/work-notes/`，采用带日期的文件名。
2. 长期规范和当前主规格放顶层；执行完的计划连同完成报告放 archive，并标明完成、部分执行、停手或替代状态。
3. 不在多份文档重复维护当前测试数、状态和能力结论；引用对应源码/证据即可。
4. 搬移文件时同步更新 Markdown 链接、仓库引用、交接中的绝对路径和生成命令。
5. 生成快照保留读数口径说明；目录搬移只更新引用，不改评测数字与历史结论。
6. 阶段目录是统一收纳处，不是可自动删除的缓存；清理内容须另有明确范围。

评测报告默认输出已调整到 `docs/work-notes/eval-report.md`；也可以通过 `--out` 显式指定其他落点。
