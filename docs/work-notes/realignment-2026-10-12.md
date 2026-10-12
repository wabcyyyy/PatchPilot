# PatchPilot Reality Audit 与校准任务卡

状态：完成；日期：2026-10-12。本轮以审查时工作区为基线，保留已有 dirty。

## 范围与依据

依据当前升级 SPEC G16、ADR-0009/0010/0014/0015，校准实现与文档，不新增产品功能。
范围：`app/task_spec.py`、`app/api/{service,preflight,schemas}.py`、
`app/graph/{acceptance,nodes,runner,plain_loop,resources}.py`、`app/evals/{driver,provenance}.py`、
`app/adapters/pytest_adapter.py`、`app/tools/{execution,registry}.py`、`app/diagnosis/proposal.py`；
现有 `tests/test_{acceptance,frozen_policy,preflight,backend,budget_reservations,tools}.py`；README、docs 索引、项目企划书、
慢用例登记 `tests/slow_tests.txt`（按新增真实执行用例的实测耗时登记，不改断言）；
ADR-0009、interview-guide 全目录及其生成 HTML、`scripts/build_interview_site.py`；本卡与 PROGRESS。

## Current Architecture

CLI 是 HTTP 客户端。路由 → TaskService → TaskSpec/hash/输入冻结/预检 → SQLite + 锁 + 线程池。
graph runner → TaskNodes → prepare/baseline/localize/[diagnose]/plan/propose/apply/verify/finish，
失败候选经 rollback 重规划。各模型阶段复用 run_plain_loop。
plain driver 单轮执行同一工具循环，另有独立 baseline/verify 编排；不提供 graph 的审批恢复流程。
注册工具 → AST/字面检索 → 有界回执/原文 evidence → 平台观测 memory → 块补丁编译 →
门禁 → git apply/语法及落点复检 → 本地或容器 pytest → JUnit 具体身份匹配 →
共享 final_acceptance。graph 另绑定冻结候选 hash 与新 verification_attempt_id。
Tracker 写 JSONL 并实时入 SQLite；report.json 是引擎取证，tasks 是服务生命周期事实。
恢复由 checkpoint + loop/session/memory + candidate + ledger 共同决定，写阶段重验。

## Drift Findings（修改前）

| 级别 | 位置、问题、调用关系 | 建议 |
|---|---|---|
| P1 | runner/driver 在读受理契约前建账本；pytest adapter 始终读全局后端；nodes/driver 的平台测试未下发 ctx 超时/后端；service 建模型仍读当前名字 | 按 G16 从受理契约重建任务配置，再创建账本、模型与执行器 |
| P1 | custom create_task → preflight 在无 env.image 时始终宿主 collect，即使后端为 docker | Docker 预检仅查 daemon，依赖/双集事实由容器 baseline 确认，不导入宿主 conftest |
| P1 | ledger_from_settings 将损坏快照当不存在，test_corrupt_ledger_state_falls_back_to_fresh 只检查归零；恢复可能重授预算并重新认证 | 保留原文件，资源标 unknown，阻止新请求并禁止 resolved；加强现有回归，不放松旧断言 |
| P1 | verify后检查deadline，但共享终局helper只复查token；门禁/diff/候选处理期间越时仍可能resolved | 共享验收直接复核当前deadline，两引擎传实际值，保留已通过验证证据 |
| P2 | interview-guide 把真实 SWE 导入/消融写成未执行；把 run_command 的 PID/提权限制套到 pytest 容器 | 区分历史实验、当前机制、两条容器命令，不宣称官方成绩 |
| P2 | README 快速开始与 VALIDATION 冲突；企划书同时自称历史与唯一规范，仍列初版工具/状态/表 | 更新当前可执行口径，阶段规划明确历史 |
| P2 | registry.execute 将签名绑定和handler放进同一try，handler内部TypeError仍被误标bad arguments；注释与精读材料声称已分开 | 分离绑定/执行异常范围，保留拒绝/轨迹行为，补业务错误分类回归 |
| P3 | _matches_requested 无 Python 调用，仅 ADR 子串锚点；ErrorResponse 未挂任何 route/handler | 删除，锚点指向 all_passed 中实际 case_matches 调用 |

未确认 P0；静态审查不是安全证明。测试侧已有 frozen profile 测试未覆盖后端/账本漂移，
FakeLLM 可验证编排和护栏，不能从回放成功推出真实修复率。

## Legacy Map

| 分类 | 实现与调用证据 |
|---|---|
| ACTIVE | graph/plain：API、run_single、tests 均调用；共享 loop、acceptance、gates、候选/恢复、SQLite/工具注册均在真实链路 |
| LEGACY BUT REQUIRED | legacy_v1：请求 schema/CLI/旧契约显式支持；FakeLLM legacy plan：旧回放使用；fold_output：scripts/context_replay、measure_context_counterfactual、probe 与测试用于历史比较；旧报告/SQLite 迁移读法保留历史数据 |
| DEAD | 私有 _matches_requested：全仓 Python 无调用；ErrorResponse：无 import、response_model 或异常 handler 引用 |
| UNCERTAIN | reset_workspace：plain 默认工具表仍可达，但 graph 阶段不给；ContextAssembler.invalidate/readings：有测试、暂无生产使用；保持以免误删公开接口 |

## Claim vs Reality

| Claim | Actual implementation | Test evidence（既有，不等于本轮执行） | Status |
|---|---|---|---|
| 严格 resolved | 非空 diff、验证双集、门禁、资源、取消合取；graph 另有候选绑定 | test_acceptance/test_f1_budget/test_candidate_checkpoints | VERIFIED |
| 完整测试身份 | file/class chain/name/参数化解析，拒缺失/歧义/跳过 | test_test_identity | VERIFIED |
| 冻结策略 | 窗口/ctx 有接线，执行后端/初始账本/模型仍漂移 | test_frozen_policy 原测试主要验证 profile/ctx | PARTIAL |
| 恢复 | graph 四态恢复，写阶段候选重应用重验；plain 不自动恢复 | test_candidate_recovery/test_resume_crash | VERIFIED |
| 容器隔离 | 默认 local；pytest 与诊断命令 docker 参数不同；compose socket 有边界 | test_backend/test_docker/test_command_profiles | PARTIAL |
| 从未实跑 SWE/消融 | 历史真实批记录存在；新增实验机制不等于已补跑实验 | work-notes/swe-ablation-evidence-2026-10-07.md；test_swebench | CONTRADICTED |
| 提交/push 前本地全量 | AGENTS/VALIDATION 要求快速定向，完整回归交 CI | verify.ps1 | STALE |

## 验证与交付记录

初始 verify.ps1 未执行检查：创建 D:/tmp/ai-validation 被拒，收据
`.validation/20261012-132539-5c443cb2.json` 为 incomplete，不算通过。
最终改动、收据与剩余边界在交付时补记；禁止付费实验、本地全量与额外功能开发。

# PatchPilot Realignment Report

## Removed Legacy

删除私有 `_matches_requested`（Python零调用，只有旧ADR锚点）与未接线 `ErrorResponse`。
真实身份判定仍由 all_passed → case_matches/parse_node_id 执行；统一错误字典仍由路由处理。
既有 `ResourceLedger.to_state` 删除属于开始前 dirty，本轮不计成果。

## Fixed Drift

`settings_for_policy` 从已受理策略创建任务配置副本，graph/plain 初始账本与模型 runtime
均先采用冻结策略；服务按受理模型名与输出上限建客户端。graph 保留受理 max_rounds/max_turns，
创建 TaskSpec 时记录实际 arm，而不硬写 agent；plain 双跑按冻结开关。
pytest 适配器、平台基线/验证/复核、diagnose 与候选分支显式传后端/超时；工具 run_tests 同口径。
provenance 的配置/协议/容器/预算身份使用实际任务配置，防执行正确但报告写成全局配置。
共享循环注释不再称未来待迁移的教学实现；runner恢复注释改为实际拒绝/重验行为。

## Correctness Fixes

Docker custom 预检不在宿主 collect；无 env.image 也按后端查 daemon，失败不回落宿主。
损坏/不支持的账本文件保留，资源标unknown，阻止新请求，终局不得resolved；不重授未知历史额度。
工具 handler 内部TypeError不再被误判为参数绑定失败；保持失败回执、轨迹与拒绝行为。
plain损坏TaskSpec不再静默丢弃契约改读当前配置。
共享终局acceptance补deadline复核，阻断VERIFY后到终局之间的越时窗口；用户等待顺延后的实际值由graph传入。

## Tests Updated

test_frozen_policy新增graph/plain真实Git/pytest链的后端、超时、双跑、预算与报告身份漂移回归，
并拒损坏plain契约。test_preflight新增Docker无题目镜像时禁止宿主执行的正反两例。
test_budget_reservations加强原损坏快照用例：保留原有读数断言，新增unknown/拒请求/拒验收/保全文件。
test_tools新增业务TypeError分类回归。没有降低门禁、签名或断言来凑绿。
test_acceptance与graph finish新增验证已绿但终局deadline过期的回归。
未删除保护当前语义的测试；未新增测试框架。

## Docs Updated

README/项目企划书/docs索引校准判定、两引擎差异、状态/工具/API/数据字段、幂等、隔离与验证节奏。
企划书早期阶段规划明确历史；ADR-0009身份锚点转到实际调用。
interview-guide逐篇校准并重建HTML：保留原型局限，纠正“未实跑SWE/消融”、容器参数混用、
固定测试数/代码行号/代码量与AI比例推断；区分历史实跑、离线回放和当前未复验。
未改历史实验数字、CI读数、发布证据或已归档原文。
HTML人工检查发现来源Markdown与site目录相差一层，生成器同步修复对指南外相对链接的转换。

## Remaining Uncertainty

- reset_workspace 是注册工具且plain可达，graph不开放；装配缓存辅助方法有测试，暂不删。
- legacy_v1、旧回放、旧报告/SQLite迁移和历史fold_output有明确用途，保留。
- 本轮冻结回归覆盖所修字段，不宣称每个全局读取都已改造：旧契约缺字段按兼容规则回退，
  外部价格覆盖文件、默认镜像标签/服务端模型版本与当前工具/提示实现仍需要身份复核；
  有记录不代表具备旧运行代码/镜像的完整重建能力。
- 默认local、无通用工具输出秘密过滤、恶意JUnit/测试过拟合、有限真实样本、单进程服务均为现有限制。
- 完整恢复/交互/分支/SWE与平台跨环境回归留CI；本轮不补付费实验、真实Docker发布证据或生产化功能。

## Validation

初始失败是临时目录访问问题；第一次定向失败为新增夹具缺max_turns和Ruff提示，均已修正。
定向配置漂移收据：`.validation/20261012-133445-8bc1b476.json`，相关文件8项通过。
账本及工具自动选相关回归：`.validation/20261012-134211-1cf3412f.json`，79项通过。
最终核心快速收据：`.validation/20261012-135906-ff7df0ee.json`，diff/AST/Ruff/格式检查通过，
七份相关测试105项通过，执行约57秒，73份未选回归留CI。唯一warning为既有TestRunResult导入触发的pytest收集提示。
Windows文档换行与分支无用变量曾被静态检查指出并修复，失败收据保留，不冒称当时通过。
HTML smoke：16篇文档，内部锚点及本地文件链接全部可解析；生成器修复后只补文档/生成器定向检查，
补充收据：`.validation/20261012-140231-0101364f.json`，生成器静态/Ruff/格式、文档锚点与慢登记检查通过（10项）。
不因登记收据或慢标记重跑核心测试。完整回归待CI。
Commit：本卡所在 `fix(realignment)` 提交，可用 `git log -1 --format=%H -- docs/work-notes/realignment-2026-10-12.md` 定位。
