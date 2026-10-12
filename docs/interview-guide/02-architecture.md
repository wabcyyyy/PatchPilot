# 02 · 当前架构与取舍

## M1 · API 与任务受理

FastAPI 同步路由校验输入，TaskService 构造 TaskSpec 完整哈希作幂等键；在途同契约返回原任务。
custom 输入冻结后预检，契约双写到文件/SQLite，再交线程池。CLI submit 通过 HTTP 调服务。
API 按单进程原型设计，默认鉴权/仓库白名单未启用，多租户与负载能力未证明。
代码：api/routes.py、service.py、preflight.py、schemas.py；测试：test_custom_task、test_preflight。

## M2 · graph 与 LangGraph

builder 定阶段/条件边，节点闭包持有工具上下文，可序列化 TaskState 进 checkpoint。
repair：prepare/baseline/localize/plan/propose/apply/verify/finish；issue-only 多 diagnose；失败经 rollback 重规划。
图提供明确阶段边界与恢复位置，平台仍要实现工具循环、预算、候选重应用与重验。
选择代价是闭包重建与阶段状态维护；不能声称框架自动保证恢复正确。
代码：graph/builder.py、nodes.py、runner.py；测试：test_graph、test_plan_invariants。

## M3 · 共享工具循环与 plain

run_plain_loop 发请求、结算 usage、执行阶段工具、维护消息/快照、处理文本完成协议与取消。
graph 的模型阶段和子任务都复用它。plain driver 单轮运行此循环，另行编排基线和平台验证。
两引擎共享 tools/pytest adapter/acceptance，但 graph 的审批、候选恢复与多轮编排不属于 plain。
代码：graph/plain_loop.py、evals/driver.py；测试：test_plain_loop、test_driver、test_frozen_policy。

## M4 · 工具与能力边界

registry 同源产生 schema 与执行入口，阶段白名单限制可见与可执行集合；finish 为完成协议工具。
候选补丁写入口 apply_patch，另有 scratch、假设记忆与问答持久写入。run_tests 只选平台预定义集合。
reset_workspace 在 plain 全量工具表可达，graph 阶段未开放；不因一个入口无调用就删公开工具。
恢复策略区分 safe_replay/state_check/no_rerun；未知工具保守不重跑。
代码：tools/registry.py、base.py、paths.py；测试：test_tool_capabilities、test_tool_recovery。

## M5 · 模型与配置

OpenAI 兼容 SDK 薄封装，无模型响应流式；SDK/平台重试互斥，协议错误携带计量事实。
能力显式声明与来源，不按模型名猜窗口；served_model 是观测，不证明别名背后的服务版本。
TaskRuntime 冻结窗口/输出能力；settings_for_policy 给每任务配置副本，保留受理预算/后端/超时/模型名。
旧契约缺字段走兼容回退，外部价格文件与服务端版本不能仅靠 profile 名保证不变。
代码：llm/openai_client.py、usage.py、profiles.py、agent/runtime.py、task_spec.py；测试：test_llm、test_frozen_policy。

## M6 · localization / evidence / memory / 仓库规则

AST 骨架 + rg 文件预筛 + Python 字面逐行匹配 + 符号跳转，不是向量 RAG，无召回质量成绩。
工具回执先结构化裁剪再序列化，原文落 evidence，read_evidence 按工件游标分页。
TaskMemory 分平台观测与引用假设，修改后引用可 stale；ContextAssembler 按阶段/轮次幂等装配。
仓库说明从输入副本加载，目录说明按观测激活；规则进入上下文但不进入权限判定。
没有跨任务长期记忆。容量不够且保护区压不动时停止，不把软阈值当任务累计预算。
代码：context/、tools/search.py、symbols.py、memory.py；测试：test_evidence_store、test_task_memory、test_repo_instructions。

## M7 · 执行器与 Docker

local_runner 用 argv/Popen、环境白名单、超时进程树清理；默认 local 共享宿主权限，目录副本不是沙箱。
pytest Docker：--rm、网络、内存/CPU、workspace/reports 挂载与 uid 对齐。
诊断 run_command Docker 另带 PID 限额、no-new-privileges；不能把其参数套到 pytest 路径。
受控 profile 不接受自由 shell；write_repro 写 scratch，命令在诊断副本运行。
Docker custom 预检只查 daemon，不宿主 collect；容器内依赖/收集由 baseline 确认。
默认执行器镜像依赖有限，题目 env 可使用自带镜像；compose socket 是部署边界。
代码：executor/、tools/commands.py；测试：test_backend、test_command_profiles、test_docker。

## M8 · Git 与补丁

冻结输入与物化工作区为物理副本、重建 Git 基线，不是 git worktree。
模型块锚定协议编译 unified diff，经门禁、git apply --check、落点检查、apply、TOCTOU 与 Python 语法复检。
回滚先保全 diff 再 reset/clean 并验证干净。不能删用户原仓库或把失败现场伪装为空补丁成功。
代码：gitops/、tools/patching.py；测试：test_gitops、test_blockpatch、test_attacks。

## M9 · pytest 身份

JUnit file/class chain/name/参数化身份对应具体 node ID；缺失、歧义、跳过不能算通过。
退出码为零不够，平台必须重新验证声明的双集。签名规则保持现有归一化，不因文档校准改变。
重复运行一致只能检测部分 flaky，不能防住预置伪绿或证明补丁语义等价。
代码：adapters/pytest_adapter.py、test_identity.py；测试：test_test_identity、test_backend。

## M10 · 门禁与终局

五类静态补丁检查在 run_gates，命令与资源门禁各在执行路径；不是七个互相独立的最终布尔字段。
共享 final_acceptance 要求验证通过、gate passed、非空diff、within_budget、未取消。
graph 还比当前 diff/候选 hash、冻结 manifest 与本次 verification_attempt_id。plain 无该恢复链。
规则可审计但不能防住指定测试上的过拟合；不让模型 judge 代替平台判断。
代码：graph/gates.py、acceptance.py、candidate.py；测试：test_acceptance、test_candidate_checkpoints。

## M11 · 持久化 / 幂等 / 恢复 / 资源

tasks/trajectory_events/patches 为当前三张业务表，去重迁移另保存 duplicates 归档。
SQLite WAL 与连接线程锁；终态原子写，stage 独立进度；Redis 可选任务锁，未证明多实例领取安全。
checkpoint + loop/session/memory + candidate + ledger 决定四态恢复；写阶段重验，END只核对读报告。
账本预留/结算/未知持有，不重授；损坏账本标unknown且阻止新请求，不能当成全新额度。
费用为micro-USD估计，非真实账单；恢复deadline保留停机时间，用户等待顺延受绝对寿命封顶。
代码：storage/、graph/resume.py、resources.py；测试：test_candidate_recovery、test_budget_reservations。

## M12 · 用户交互与子任务

graph 问题/回答/纠偏落盘，回答与批准是可信用户事件，不能修改冻结测试/金额/权限边界。
headless 需要澄清或批准时如实待审，不能自动伪造同意。会话修补孤儿 tool_calls 只给明确占位回执。
三种冻结角色，单层有限委派、独立会话与父预算切分，默认关闭；子结论不能覆盖父验收。
角色模型工厂由service/CLI注入，缺独立Fake脚本不能偷偷沿用父游标或发付费调用。
代码：agent/control.py、session.py、subtasks.py；测试：test_waiting_input、test_subagent_wiring。

## M13 · 评测

自建题、版本化指标、manifest批定义、控制身份与观测分离；one_shot是评测CLI臂，API拒绝。
SWE适配器复用平台pytest与验收，可用官方镜像，但不是官方harness/成绩。
Fake回放含参考答案；历史真实SWE/消融/重复实验有记录与负结果，当前升级未补跑。
见 [评测证明力](05-evaluation.md)，不要把历史分数当当前能力。
