# 05 · 评测证据与证明力

必须分开回答：平台如何判定补丁成立，以及真实模型在什么样本上取得什么结果。

## 1. 离线测试与 FakeLLM

模型交互使用 FakeLLM，不依赖网络；真实 Git、pytest 子进程、JUnit、SQLite 与崩溃注入用于验证平台接线。
参考补丁被嵌入回放脚本，所以 fake 的 resolved 不能宣读为模型修复率。
legacy 计划模式自动识别计划请求并返回预制文本，不能验证真实模型计划遵循；strict 模式与协议测试补这层边界。

| 检查 | 证明的事实 | 局限 |
|---|---|---|
| test_attacks / test_test_identity | 门禁与具体身份匹配的已知攻击/缺失/歧义场景 | 不是任意不受信仓库的安全证明 |
| test_candidate_recovery / test_resume_crash | 候选重应用、重验、真实进程死亡恢复 | 不是所有崩溃点或多实例一致性证明 |
| test_budget_reservations / test_f1_budget | 预留/结算/未知用量与终局拒绝 | 估算不是 tokenizer 真值，也不是 provider 账单 |
| test_frozen_policy | graph/plain 配置漂移后仍走受理后端、双跑和预算 | 不替代每个外部端点/部署的复验 |
| test_bugset / test_driver / test_graph | 自建题基线和平台闭环 | 合成小仓库与答案回放不证明泛化 |
| test_backend / test_docker | 路由、容器命令以及可用环境下的容器 smoke | mock 路由和真实 Docker 证据必须分开 |
| test_docs_anchors | 引用子串、注册清单、相对链接一致 | 子串存在不证明完整能力语义 |

用例总数以对应提交/环境的收据为准，不维护当前通过数或 CI 连绿数。

## 2. 真实模型实验确实存在，但强度不同

| 材料 | 能支持什么 | 不能支持什么 |
|---|---|---|
| evidence/eval/eval-report-real.md | 一份早期真实模型批次快照 | 缺模型名/成本/provenance，不作当前修复率依据 |
| [SWE 消融证据](../work-notes/swe-ablation-evidence-2026-10-07.md) | 导入外部真实题、真实模型运行、双跑复核、两臂与后续重复噪声实验的历史记录，含负结果 | 最早两臂跨 graph/plain、判定强度不同；小样本/选择偏差；不能推广为当前版本收益 |
| [付费批预登记与停手](../archive/paid-batch-preregistration-2026-10-08.md) | 预登记题单、重复设计、负结果与停止条件 | 预登记不等于实施全部目标，更不构成新付费授权 |
| evidence/agent-upgrade 的离线收据 | 对应当时提交、dirty 与环境下的发布检查 | 不是当前提交 CI 结果或官方 benchmark 成绩 |

本轮只读实验记录并做离线校准，没有付费请求、没有重跑 SWE 批次。
原始 runs 不随 Git 全部入库；记录可证明当时报告了哪些结果，独立复核仍需要完整产物与对应环境。
“效果证据为零”和“已证明循环普遍优于单发”都超出事实。

## 3. SWE-bench 接入真实边界

swebench.py/import_swebench.py 将元数据、源码/test_patch 和 env 转成本地任务；可使用官方题目镜像，
复用本平台 pytest adapter、门禁和验收。使用官方镜像不等于使用官方评测 harness。
当前判定是具体失败集与抽样回归集，不是官方完整 PASS_TO_PASS 覆盖；样本、筛选、协议与提交形态自定。
历史实跑不等于提交了官方成绩；仓库没有 bugs/SWE-* 也不意味着从未在 runs/ 下导入运行。

## 4. 指标与事实源

metrics_version=2；annotate_v1 仅用于显式历史重算，不删除或混算。
resolved 真值来自平台 acceptance，metrics 做统计；strict 定位为 bool(touched) and touched <= expected，
仍可能只覆盖参考文件的一部分，所以 coverage 必须一起读。
latest_per_bug 是历史每题/臂取最新口径；重复实验使用 pair_runs 保留 (task, arm, repetition)。
manifest 是批定义，比较控制身份与观测结果；成本 micro-USD 来自 usage × 价表，非 settled_bill。

## 5. 推荐面试回答

“离线回放证明平台的已覆盖控制链路，不能证明模型效果。真实 SWE 与消融有历史实跑，
但样本有限、早期对照跨引擎、回归集抽样，后续还有负结果。当前升级没有重新实跑，
所以我讲机制与边界，不报官方解决率，也不把历史分数当当前版本成绩。”

未来扩展可以是同引擎分层抽样、隐藏测试、人类盲审、预登记重复；这些不是本轮新增功能或已完成能力。
