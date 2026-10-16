# PatchPilot 项目企划书

本文维护项目范围和当前架构概要。升级行为以 [当前 SPEC](PatchPilot修复Agent升级SPEC-2026-10-10.md) 与对应 ADR 为准；源码与测试用于验证接线，不能用历史规划证明当前能力。

第 10 节起的阶段计划、旧评测目标与简历草案保留为早期规划背景，不作为完成证据。实际实验见 `work-notes/` 的带日期记录，开发验证入口见根目录 `verify.ps1` 与 `validation-rules.json`。

## 1. 项目定位

### 1.1 项目名称

**PatchPilot:面向软件仓库的测试失败诊断与补丁验证平台**

### 1.2 一句话描述

给定一个本地软件仓库和测试失败描述,Agent 自动调查相关代码、生成补丁、在隔离工作区执行测试,并根据测试结果和变更范围判断补丁是否可靠。

### 1.3 参考项目与借鉴边界

| 项目 | 借鉴什么 | 明确不做 |
|---|---|---|
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | 工具循环、trajectory 记录、仓库操作工具抽象 | 不照搬完整工具集与配置体系 |
| [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) | 轻量实现的结构,作为阅读入口 | 不复制代码 |
| [Agentless](https://github.com/OpenAutoCoder/Agentless) | "定位 → 上下文 → 补丁"的阶段化思路 | 不做全流程复现 |
| [SWE-bench](https://github.com/SWE-bench/SWE-bench) | 评测判定思路(失败测试转通过 + 通过测试不回退) | 不承诺 SWE-bench 跑分 |

原则:独立实现适合应届生理解和展示的工程子集,不声称复刻完整 SWE-agent,不声称达到 SWE-bench 水平。

### 1.4 与 Travel-Assistant 的区分

```text
Travel-Assistant:面向旅行规划的业务 Agent,重点是多轮状态、外部 API 工具和确定性行程变更。
PatchPilot:面向研发流程的开发者 Agent,重点是仓库理解、命令执行、补丁验证、回滚和质量门禁。
```

## 2. 用户与使用方式

用户提交:

- 一个平台任务集 bug_id 或可访问的本地仓库目录；HTTP 未提供任意 commit checkout;
- 一段 Bug/Issue 描述;
- repair 模式的具体失败/回归 node IDs，或 issue-only 诊断后批准的清单；不受理自由测试命令;
- 请求 profile 与最大修复轮数；执行预算由受理配置冻结，任意权限/金额扩张需新契约。

系统返回:

- 任务状态和执行轨迹;
- 修改文件和 unified diff;
- 测试前后结果;
- 修复结论:`resolved`、`needs_review` 或 `failed`;
- 失败原因、资源消耗和人工复核建议。

典型示例:

> 仓库中的日期解析函数在输入空字符串时抛出异常,请修复,并保证原有测试和新增回归测试通过。

## 3. 总体架构

```text
客户端(CLI / HTTP)
        │
┌───────▼─────────────────┐
│ API 层(FastAPI)         │ 任务创建、状态查询、报告下载
└───────┬─────────────────┘
        │
┌───────▼─────────────────┐
│ 编排层(LangGraph)       │ 状态机 + 条件分支 + 轮数循环 + 预算控制
└───────┬─────────────────┘
        │
┌───────▼─────────────────┐
│ 工具层(注册表与阶段能力集合) │ 每次调用记录轨迹
└───────┬─────────────────┘
        │
┌───────▼─────────────────┐
│ 执行层                  │ Git 快照/diff/回滚 + Docker 临时容器跑 pytest
└───────┬─────────────────┘
        │
┌───────▼─────────────────┐
│ 存储层                  │ SQLite(任务/轨迹/补丁/测试结果)+ Redis(锁/TTL)
└─────────────────────────┘
```

模块职责:

| 模块 | 职责 | 主要技术 |
|---|---|---|
| API 层 | 任务创建、状态查询、报告下载 | FastAPI + Pydantic |
| 编排层 | 状态机、条件分支、轮数循环、预算控制 | LangGraph |
| 工具层 | 七个 Agent 工具与调用记录 | Python |
| 执行层 | Git 快照/diff/回滚、容器化测试执行 | GitPython/subprocess + Docker |
| 适配器 | 语言/框架差异封装,首版只有 pytest | pytest |
| 存储层 | 持久化 + 任务锁、幂等、TTL | SQLite(→PostgreSQL)+ Redis |
| 评测 | Bug 集管理、指标计算、报告生成 | pytest + 脚本 |

关键设计决策:

- **状态机先行**:先定义状态与转移规则(见第 4 节),再写节点代码;
- **工具最小集**:以 registry 的注册表为准，各阶段只暴露对应能力集合;
- **判定规则显式化**:`resolved` 必须满足共享 acceptance 的合取条件(见 4.3),不允许"返回码为 0 即成功"。

## 4. 核心状态机

### 4.1 当前 graph 执行链

`prepare → baseline → localize → plan → propose → apply → verify → finish`。
issue-only 任务跳过初始 baseline，在 localize 后插入 diagnose：提出具体失败/回归测试 →
平台在输入副本上复核红/绿基线 → trusted_user 批准冻结清单 → plan。

门禁拒绝或验证失败经 rollback 保全现场、复位、消耗一次候选轮次，再回 plan；预算耗尽停止。
未知用量、双跑不一致、缺可信恢复身份等转 needs_review。服务生命周期另有 QUEUED/RUNNING/
WAITING_INPUT/CANCELLED；图 stage 不等于数据库 lifecycle status。
权威拓扑见 `app/graph/builder.py`，路由见 TaskNodes，服务终态见 Repository。

plain 是单轮基线 → 共享工具循环 → 独立平台验证 → 共享验收，不包含 graph 的多轮候选、
诊断审批与自动恢复编排。LangGraph 提供阶段拓扑与 superstep checkpoint；实际工具循环、
资源计量和恢复重验由平台实现，不能仅凭 checkpointer 宣称恢复正确。

### 4.2 修复判定规则

`resolved` = 当前补丁非空 ∧ 原失败测试全通过 ∧ 回归测试全通过 ∧ 门禁全通过
∧ 资源 within_budget ∧ 未取消。双跑开启时必须身份集合与通过结果一致。
共享唯一实现为 `app/graph/acceptance.py`；模型 finish 只是阶段声明。

graph 另外核对当前工作区 diff 与冻结候选 hash 一致，每次验证刷新 verification_attempt_id；
写阶段恢复从候选重应用并重跑门禁/测试，绝不复用旧成功布尔。
plain 复用严格合取验收，但不声明拥有 graph 的候选工件恢复链。

### 4.3 执行与资源边界

TaskSpec 冻结输入、具体 node IDs、回放、引擎、实验臂、模型身份与有效策略。
受理后启动使用任务配置副本；恢复优先读原账本、deadline 和候选，不能重授额度。
local 是默认执行后端，目录副本不构成进程沙箱；Docker pytest 带网络/内存/CPU 限制，
诊断命令 Docker 另带 PID 限额和 no-new-privileges。Docker custom 预检不在宿主导入目标测试；
它只查 daemon，真实收集/依赖与基线由容器执行确认。

## 5. Agent 工具协议

完整注册表在 `app/tools/registry.py`：list_files/search_code/find_symbol/describe_file/read_file/
read_evidence/apply_patch/run_tests/write_repro/run_command/git_diff/reset_workspace/update_memory/
propose_acceptance/ask_user/delegate；finish 是循环完成协议工具，另计。
阶段 schema 与执行能力同源，注册并不意味着每阶段可用。

apply_patch 接收块锚定协议，平台编译为 unified diff；不让模型计算行号。
run_tests 只接受预定义 test_set；run_command 只接受冻结 profile 与受限参数，不接受自由 shell。
write_repro 写任务 scratch，update_memory 只登记带引用假设；它们不构成候选补丁写入口。

tool_started/完成回执及模型响应写 JSONL 并实时入 SQLite；有界 JSON 回执附原始 evidence 引用。
任务 memory 由平台登记真实观测，模型自述不升级为已验证事实。仓库说明只进上下文，
不扩大工具权限或验收契约。当前行为见 ADR-0012/0013/0015。

## 6. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| tasks | id, idem_key, task_spec_json/hash/schema_version, status, stage, last_event_at, run_dir | 任务主表 |
| trajectory_events | id, task_id, round, state, tool, request_id, input, output_summary, duration_ms, error, timestamp | 按第 5 节格式追加 |
| patches | id, task_id, round, diff_path, changed_files, gate_result, applied | 每轮补丁与门禁结论 |

原设计的 `test_runs` 与 `evaluations` 两表已裁撤:前者零生产读方(每次 pytest
完整结果落在 run_dir/reports/*.xml,库里只会是空表),后者与 tasks/report.json
三处分裂(读口径:tasks=服务生命周期真相,report.json=引擎判定取证)。
沿革与理由见 `app/storage/db.py` 模块注释(P1-5/P3-8 整改)。

重复轨迹迁移另保留 trajectory_events_duplicates 归档表；旧 test_runs/evaluations 表不会作为当前读写事实源。SQLite WAL + 单连接线程锁，按单进程原型部署，不声明多实例领取安全。

## 7. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建任务,返回 task_id;按完整 TaskSpec 哈希幂等(未到终态时返回原任务) |
| GET | `/api/tasks/{task_id}` | 查询状态与进度(当前状态、轮数、耗时) |
| GET | `/api/tasks/{task_id}/trajectory` | 分页返回轨迹事件 |
| GET | `/api/tasks/{task_id}/report` | 下载最终报告(JSON/Markdown) |
| POST | `/api/tasks/{task_id}/cancel` | 取消任务,保留现场 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/tasks` | 列举任务 |
| GET | `/api/capabilities` | 可受理策略；one_shot 如实标记评测 CLI 专用 |
| POST | `/api/tasks/{task_id}/acceptance` | 批准冻结验收清单 |
| POST | `/api/tasks/{task_id}/messages` | 回答或纠偏，不能扩张冻结权限 |
| GET | `/api/tasks/{task_id}/question` | 当前问题与输入版本 |
| GET | `/api/tasks/{task_id}/events` | SSE，以事件 ID 续传 |

约束:任务执行走后台任务,不在 HTTP 请求里同步跑完;错误统一为 `{code, message, task_id}` 结构。

## 8. 目录结构(建议)

```text
patchpilot/
├── pyproject.toml
├── app/
│   ├── api/          # FastAPI 路由与 Pydantic Schema
│   ├── graph/        # LangGraph 状态、节点、条件边
│   ├── tools/        # 注册工具、阶段能力与轨迹
│   ├── executor/     # subprocess / Docker 测试执行
│   ├── gitops/       # 快照、diff、应用补丁、回滚
│   ├── adapters/     # pytest 适配器(预留 maven/jest)
│   ├── storage/      # SQLite / Redis 访问层
│   └── evals/        # 评测脚本与指标计算
├── bugs/             # 自建 Bug 任务集(仓库 + issue + manifest)
├── tests/            # 项目自身测试
├── docker/           # 执行器镜像与 docker-compose
└── docs/             # 设计笔记、复盘与评测报告
```

## 9. 质量门禁与安全规则

门禁清单(全部通过才允许从 APPLY_PATCH 进入 VERIFY):

1. **格式门禁**:diff 能被 `git apply --check` 接受;
2. **文件门禁**:未修改任何测试文件(`tests/`、`test_*.py`、`*_test.py` 等规则可配置);
3. **路径门禁**:所有修改路径在仓库根内,无 `..` 穿越,无绝对路径;
4. **范围门禁**:修改文件数不超过上限(建议 5),新增文件需显式声明;
5. **命令门禁**:测试命令在白名单内,禁止 shell 字符串拼接;
6. **资源门禁**:候选轮数、任务deadline、累计token/调用数及显式费用模式等按冻结策略检查;
7. **影子门禁**(2026-09-20 审计新增):新增文件不得与解释器/工具链顶层模块
   同名(pytest/stdlib 等)——否则 `python -m pytest` 的 sys.path[0]=工作区
   会让影子包伪造测试报告,架空判定层。

执行隔离：默认local，在独立目录工作区运行pytest子进程，不是进程沙箱。
选Docker时测试挂载当前工作区并写外部reports目录，带网络/内存/CPU限额、--rm；
不是只读挂载，测试本身可能写工作区。受控诊断命令在另外的诊断副本运行，Docker另带PID/提权限制。

失败处理：失败候选保全现场后回滚，回plan修订；报告/diff/轨迹/JUnit保留。
成功任务可回收workspace与checkpoint，失败/取消/待审现场保留；输入副本也属回收策略中的工件，
不宣称所有快照永久保存。实际可弃集见 app/api/recycle.py。

## 10. 早期阶段规划（历史背景，以下不作当前完成证据）

### 阶段一:理解 mini-SWE-agent(第 1—3 周,前置学习模块 L3)

**目标:** 看懂参考实现的工具循环与记录方式。

**任务:**
- 在本地或 Codespaces 跑通一个简单任务;
- 观察模型如何调用搜索、读取、编辑和测试工具;
- 阅读工具定义、任务循环和 trajectory 格式;
- 画出"模型—工具—工作区—测试"的关系图。

**产出:** 一份运行记录、一次完整 trajectory、一份架构笔记。

**验收:** 能不看资料默写循环伪代码;能指出状态管理、执行、记录分别在源码的哪部分。

### 阶段二:实现本地单任务闭环(第 2—4 周,前置学习模块 L0—L4)

**目标:** 输入本地 Python 仓库和 Bug 描述,能走完"搜索 → 读码 → 补丁 → pytest"并输出报告。

**任务:**
- 完成文件搜索、文件读取、补丁应用和 pytest 执行;
- 先用简单 Agent 循环跑通,再迁移到 LangGraph;
- 保存每轮消息、工具调用和测试输出。

**产出:** 能修复 3—5 个自建简单 Bug,并输出 diff 和测试报告。

**验收:** 每个修复都有 trajectory 落盘;从 Bug 描述到报告全程无需人工干预。

### 阶段三:加入质量门禁(第 5—6 周,前置学习模块 L2/L4)

**目标:** 把"测试通过"和"补丁可信"区分开。

**任务:**
- 固定仓库 commit 和测试失败基线;
- 限制可修改文件和允许执行的命令;
- 检查补丁是否修改测试文件或越权访问路径;
- 实现最大修复轮数、超时、失败回滚和人工审核状态;
- 把 4.3 的判定规则代码化。

**产出:** 至少一个被拦截的危险/越权案例,和一个回滚案例。

**验收:** 判定规则有单元测试覆盖;拦截与回滚事件在轨迹中可查。

### 阶段四:服务化与任务管理(第 6—8 周,前置学习模块 L5—L7)

**目标:** 从 API 创建任务到查询最终报告的完整服务化流程。

**任务:**
- FastAPI 提供任务创建、状态查询和报告下载接口(见第 7 节);
- SQLite 保存任务、轨迹、补丁和测试结果(见第 6 节);
- Redis 实现任务锁、重复任务幂等和状态 TTL;
- Docker Compose 启动 API、数据库和执行器。

**产出:** 可通过 API 完成任务全生命周期的演示。

**验收:** 服务重启后任务与轨迹不丢;重复提交被幂等拦截;至少一次完整 VERIFY 在容器内完成。

### 阶段五:评测与扩展(第 8 周起)

**目标:** 用数据说话,形成可复现的评测报告。

**任务:**
- 构建 20—40 个小型 Python Bug 任务(构成见 11.1);
- 分类记录定位成功、补丁可应用、最终修复、回归引入和安全拦截;
- 使用 pytest 和 GitHub Actions 自动运行回归;
- 之后才考虑 Java/Maven 或 JavaScript/Jest 适配器;
- 根据真实失败样本增加代码结构检索、上下文压缩或模型对比。

**产出:** 可复现评测报告和失败案例复盘。

**验收:** 评测脚本一键重跑出同版报告;每个指标都有判定脚本而非人工标注。

## 11. 评测方案

### 11.1 Bug 集构成

每个任务一个目录:

```text
bugs/
└── BUG-007-date-parse-empty/
    ├── repo/          # 含固定 commit 的小仓库
    ├── issue.md       # 面向 Agent 的问题描述
    ├── manifest.yaml  # 失败测试、回归测试、允许修改范围、预算
    └── expected/      # 判定所需信息(期望修改范围,不等于标准答案补丁)
```

分类覆盖:异常处理、边界条件、类型错误、数据访问错误,四类大致各占 1/4;难度分两档——简单(单文件单函数)与中等(需要跨文件定位)。

### 11.2 指标定义

| 指标 | 定义 | 判定方式 |
|---|---|---|
| 定位成功率 | 是否找到与失败相关的代码位置 | 补丁触碰文件与期望修改范围相交 |
| 补丁应用率 | 生成的 diff 能否安全应用 | `git apply --check` 通过 |
| 最终修复率 | 原失败测试和回归测试均通过且范围合法的任务比例 | 4.3 判定规则 |
| 回归引入率 | 修复后新增失败测试的任务比例 | PASS_TO_PASS 集合出现新失败 |
| 越权拦截率 | 修改受限文件或执行受限命令被拦截的比例 | 门禁拦截记录 / 构造的越权样例数 |
| 平均修复轮数 | 每个任务的 Agent 尝试次数 | trajectory 统计 |
| 平均耗时/Token | 单任务资源成本 | 运行记录统计 |

### 11.3 报告要求

- 同时展示成功和失败任务;失败任务必须附复盘(发生在哪个状态、失败签名、根因);
- 不使用没有测试集来源和判定规则的数字;
- 报告由脚本生成,保证任何人一键可复现。

## 12. 风险控制与项目边界

- 首版只实现 Python/pytest 适配器,平台接口预留其他语言,不同时实现多种语言;
- 首版使用本地自建 Bug 集,不直接承诺 SWE-bench 成绩;
- 不把项目描述成 Codex、OpenHands 或 SWE-agent 的替代品;
- 不执行用户未授权的危险命令,不连接真实生产仓库;
- 如果 Agent 只能生成补丁但无法验证结果,项目不进入简历最终版本;
- 如果没有完整轨迹、失败案例和评测报告,项目只作为学习项目保留。

## 13. 完成标准(简历准入)

满足以下全部条件后,才建议把项目放进简历:

- [ ] 能从 API 或命令行创建一个完整任务;
- [ ] 至少修复一组自建 Bug,并保留成功与失败轨迹;
- [ ] 至少有一个补丁被安全门禁拦截;
- [ ] 至少有一个任务发生回滚或转人工;
- [ ] 有自动化测试和可复现评测报告;
- [ ] 能清楚解释 SWE-agent 的设计、自己的改动和未实现部分。

## 14. 简历叙事

### 14.1 项目标题

`PatchPilot - 面向软件仓库的测试失败诊断与补丁验证平台`

### 14.2 技术栈

`Python / LangGraph / FastAPI / pytest / Docker / Git / SQLite / Redis`

### 14.3 项目描述

> 面向受控软件仓库构建测试失败诊断与补丁验证 Agent,参考 SWE-agent 的工具循环设计,编排代码定位、补丁生成、隔离执行和回归验证流程,并通过 Git diff、测试基线和质量门禁判断修复结果。

### 14.4 简历 bullet 草案

- 基于 LangGraph 编排测试基线、失败分析、代码检索、补丁应用和回归验证节点,使用状态字段记录修复轮数、失败签名、变更文件和测试结果。
- 封装仓库搜索、文件读取、unified diff、测试执行和工作区回滚工具,记录每轮工具调用、耗时和错误,支持完整轨迹复盘。
- 使用 Docker 临时工作区、命令白名单、文件路径校验和资源限制执行测试;拦截测试文件修改、越权路径和超预算任务。
- 设计"原失败测试通过 + 相关回归测试通过 + 修改范围合法"的修复判定规则,避免仅依据单次测试返回码确认成功。
- 构建包含异常处理、边界条件、类型错误和数据访问错误的离线 Bug 集,评估定位成功率、补丁应用率、最终修复率和回归引入率。

所有数字必须在项目完成后根据真实运行记录填写,不预填、不估计。
