# 00 · 当前实现事实基座

本页校准于 2026-10-12 的工作区。当前源码、对应测试与有效 SPEC/ADR 共同确认事实；
历史日志是演化证据，不是当前能力表。完整审查与交付见 [实现校准任务卡](../work-notes/realignment-2026-10-12.md)。

## 1. 当前项目

PatchPilot 是 Python/pytest 仓库的测试失败诊断与补丁验证原型，服务面是 FastAPI，
任务与轨迹落 SQLite，Redis 可选地承担任务锁。CLI submit 是 HTTP 客户端。
模型层为 OpenAI 兼容客户端或离线 FakeLLM；不做向量检索、跨任务长期记忆或官方 SWE-bench 评测。
测试数量不作为固定能力，历史发布收据只证明当时提交/dirty/环境下的结果。

## 2. 完整执行链

1. TaskCreateIn/路由 → TaskService 校验仓库与模型开关，构建完整 TaskSpec 哈希幂等键。
2. custom 输入复制冻结并核对指纹，预检环境；Docker 预检只查 daemon，真实收集由容器基线确认。
3. 契约写文件与 SQLite，再进入线程池；执行先交叉复核受理身份。
4. graph runner 从受理策略重建任务配置、模型 runtime 与账本，物化独立 Git 工作区。
5. baseline 验证失败集真红、回归真绿；localize 用 AST 骨架、符号与字面检索读证据。
6. issue-only 路径由 diagnose 提出具体测试，平台重做红/绿基线，经 trusted_user 批准后冻结清单。
7. plan 产生文本计划，propose 运行工具循环；块锚定补丁经编译、门禁、git apply 与语法/落点复检。
8. apply 冻结候选；verify 平台独立跑失败/回归集，按 JUnit 完整身份匹配，开启时再双跑比对。
9. finish 使用共享 acceptance，graph 另校验候选绑定。失败经 rollback 留证、复位、回 plan。
10. JSONL/SQLite 保存轨迹，report.json 记录引擎证据，tasks 保存服务生命周期。取消/迟到事件不能复活终态。

## 3. graph / plain 与 LangGraph

graph 明确阶段拓扑、条件边与 superstep 检查点。模型在阶段内自主选工具，不能任意改变平台阶段或 verdict。
真正模型工具循环在 run_plain_loop，graph、plain 与子任务都调用。
plain driver 是单轮基线/循环/平台验证，不具有 graph 的诊断审批、多轮候选或自动恢复编排。
两者共享工具、pytest adapter、final_acceptance；共享判定不等于功能等价。
LangGraph 本身不保证恢复正确：平台还必须重建 ctx、读候选与账本、清旧结论并重验。

## 4. 当前关键事实与局限

| 能力 | 当前事实 | 可核对位置 |
|---|---|---|
| 判定 | 非空 diff、双集验证、门禁、资源合规（终局复核deadline）、未取消合取；模型 finish 不决定 resolved | graph/acceptance.py；test_acceptance |
| 测试身份 | 文件、类链、函数、参数化，拒缺失/歧义/skip | adapters/test_identity.py、pytest_adapter.py；test_test_identity |
| 候选 | graph hash 绑定当前工作区与冻结 manifest；验证有新 attempt ID | graph/candidate.py、nodes.py；test_candidate_checkpoints |
| 恢复 | continue/revalidate/already_terminal/reject；写阶段重应用重验；无可信身份不冷启动 | graph/resume.py；test_candidate_recovery、test_resume_crash |
| 资源 | pending/reserve/settle/unknown，未知消耗继续占额度；费用是价表估计，不是账单 | graph/resources.py；test_budget_reservations |
| localization | AST/rg 文件预筛/Python 字面匹配与符号跳转，无检索质量基准 | context/repo_map.py、tools/search.py、symbols.py |
| evidence/memory | 有界 JSON + 原文分页；平台观测与模型假设分开，引用可 stale | context/evidence.py、task_memory.py；test_evidence_store |
| 仓库规则 | 冻结副本说明进上下文，目录说明按观测激活；不扩权 | context/repo_instructions.py；test_repo_instructions |
| 交互/委派 | graph 问答/批准持久化；有界单层角色子任务，默认关闭，不覆盖父验收 | agent/control.py、subtasks.py；test_subagent_wiring |
| 受理冻结 | 后端、测试超时、初始账本、runtime、模型身份与轮次沿用契约；不修改全局 Settings | task_spec.settings_for_policy；test_frozen_policy |
| Docker | 默认 local；pytest 容器网络/内存/CPU限额，诊断命令另有 PID/no-new-privileges | executor/docker_runner.py、command_runner.py |
| 服务 | 单进程原型，SQLite 线程锁、线程池、SSE 数据库轮询；Redis 不是队列/缓存 | api/service.py、events.py；storage |

目录副本不是进程沙箱。local 会执行目标仓库 conftest/模型复现代码，子进程不是平台同一进程，
但共享宿主权限。Docker 不能证明测试语义正确，compose socket 也需要部署方隔离。
全绿只是指定测试下成立，不能防住过拟合或所有恶意仓库伪造 JUnit。

## 5. Legacy Map

ACTIVE：两引擎及共享工具循环、验收、门禁、candidate/恢复、工具注册表。
LEGACY BUT REQUIRED：legacy_v1、FakeLLM 旧计划回放、旧报告/SQLite 迁移、历史实验 fold_output。
DEAD（本轮删除）：_matches_requested 私有 wrapper 与未接线 ErrorResponse。
UNCERTAIN（保留）：reset_workspace（plain 默认工具表可达、graph 阶段不开放）、装配缓存辅助方法。
ResourceLedger.to_state 的删除是本轮开始前的已有工作，不计为本轮删除。

## 6. Claim vs Reality

严格验收与 graph 恢复有代码和既有测试支撑，不能上升为全面安全保证。
原冻结策略是 PARTIAL，本轮补真实 pytest 链的环境漂移回归。
“从未导入 SWE 样本/从未实跑消融”与历史实验记录相冲突，已纠正。
“提交/push 前本地全量”已过时，日常统一 verify.ps1、90 秒、定向测试，完整回归交 CI。

## 7. 实验口径

FakeLLM 回放里包含参考补丁，证明编排、护栏与取证，不证明模型修复率。
旧 eval-report-real 缺模型名/成本/provenance，证据较弱；另有 SWE 真实模型与消融记录，
见 [实验边界](05-evaluation.md)。不能因为 runs 被忽略就断言实验未发生，也不能因为记录存在就认定当前版本已复验。

## 8. 面试表达

讲已实现的判定与恢复机制，主动说 local 默认、有限样本、官方 harness 未使用、费用估计、单机部署限制。
P0/P1 面试知识优先级不等于仓库缺陷严重性；本轮审查未确认 P0，不作“所有攻击都不可能”承诺。
