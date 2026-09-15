# PatchPilot 项目企划书

> 本文由《PatchPilot项目初步企划书与学习建议》拆分细化而来,覆盖项目定位、架构设计、状态机、工具协议、数据模型、阶段规划与评测方案。
> 学习路线、动手练习与自查标准见《PatchPilot学习计划》;项目各阶段标注了所需的学习模块前置。

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

- 一个本地 Git 仓库或指定 commit;
- 一段 Bug/Issue 描述;
- 测试命令或项目适配器;
- 最大修复轮数和执行资源限制。

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
│ 工具层(7 个 Agent 工具) │ 每次调用记录轨迹
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
- **工具最小集**:第一版只保留 7 个工具,之后按真实失败案例增补;
- **判定规则显式化**:`resolved` 必须同时满足四个条件(见 4.3),不允许"返回码为 0 即成功"。

## 4. 核心状态机

### 4.1 状态定义

| 状态 | 含义 |
|---|---|
| CREATED | 任务已创建,参数待校验 |
| BASELINE | 固定 commit,运行目标测试,记录"失败基线" |
| LOCALIZE | 分析 Issue/错误日志,检索并阅读相关代码 |
| PROPOSE_PATCH | 生成 unified diff 补丁 |
| APPLY_PATCH | 校验补丁(格式/范围/权限)并应用到隔离工作区 |
| VERIFY | 执行原失败测试与回归测试 |
| FINISHED | 正常结束(修复成功) |
| INVALID_TASK | 仓库、测试命令或任务参数不合法 |
| PATCH_REJECTED | 补丁格式错误、修改范围超限或修改了测试文件 |
| VERIFY_FAILED | 测试仍失败或引入回归 |
| BUDGET_EXCEEDED | 超过修复轮数、执行时间或资源预算 |
| NEEDS_REVIEW | 无法自动判断,转人工审核 |

### 4.2 转移表

| 当前状态 | 事件 | 下一状态 | 动作 |
|---|---|---|---|
| CREATED | 参数校验通过 | BASELINE | 固定 commit、打快照 |
| CREATED | 校验失败 | INVALID_TASK | 记录原因,结束 |
| BASELINE | 目标测试失败现象确认 | LOCALIZE | 保存失败签名 |
| BASELINE | 目标测试全部通过 | INVALID_TASK | 无失败可修,任务无效 |
| LOCALIZE | 定位到候选代码 | PROPOSE_PATCH | 记录定位结果 |
| PROPOSE_PATCH | 生成补丁 | APPLY_PATCH | 提交 diff 校验 |
| APPLY_PATCH | 校验并应用成功 | VERIFY | — |
| APPLY_PATCH | 格式/范围/权限违规 | PATCH_REJECTED | 记录违规详情;轮数未超 → 回 PROPOSE_PATCH 重试 |
| VERIFY | 原失败测试 + 回归全过 | FINISHED | 按 4.3 判定为 resolved |
| VERIFY | 原失败测试仍失败 | PROPOSE_PATCH | 轮数 +1;超限 → BUDGET_EXCEEDED |
| VERIFY | 引入新失败(回归) | PROPOSE_PATCH | 先回滚本轮补丁;超限 → BUDGET_EXCEEDED |
| 任意 | 轮数/时间/token 超预算 | BUDGET_EXCEEDED | 保留现场,结束 |
| 任意 | 无法自动判断 | NEEDS_REVIEW | 生成人工复核建议 |

循环只发生在 PROPOSE_PATCH ↔ APPLY_PATCH ↔ VERIFY 之间,由预算(最大轮数、时间、token)约束。

### 4.3 修复判定规则

`resolved` 必须同时满足:

1. 基线中失败的目标测试全部转为通过;
2. 回归测试集(PASS_TO_PASS)全部保持通过;
3. 补丁通过全部质量门禁(未修改测试文件、未越权、未超范围,见第 9 节);
4. 未触碰资源预算上限。

条件不满足时的状态映射:条件 1/2 不满足 → VERIFY_FAILED;条件 3 不满足 → PATCH_REJECTED;条件 4 不满足 → BUDGET_EXCEEDED;其余无法自动判断的情况 → NEEDS_REVIEW。

## 5. Agent 工具协议

第一版只提供 7 个必要工具:

| 工具 | 输入 | 输出 | 边界与限制 |
|---|---|---|---|
| `list_files` | 目录路径(可选 glob) | 文件列表 | 仅限仓库根内;忽略 `.git` 等目录 |
| `search_code` | 关键词、文件过滤 | 匹配行与位置 | 结果条数上限;只搜文本文件 |
| `read_file` | 路径、行范围 | 文件内容片段 | 单次读取行数上限;拒绝二进制文件 |
| `apply_patch` | unified diff | 应用结果 | 先 `git apply --check` 校验;拒绝触碰测试文件与越权路径 |
| `run_tests` | 测试命令(白名单内) | 测试报告解析结果 | 仅白名单命令;容器内执行;超时上限 |
| `git_diff` | — | 当前工作区 diff | 只读 |
| `reset_workspace` | — | 恢复结果 | 只能恢复到任务开始时的快照 |

每次工具调用记录一条 trajectory 事件(输入、输出、耗时、错误、请求 ID),落库格式:

```json
{
  "event_id": "uuid",
  "task_id": "uuid",
  "round": 2,
  "state": "PROPOSE_PATCH",
  "tool": "search_code",
  "request_id": "uuid",
  "input": {"keyword": "parse_date"},
  "output_summary": {"matches": 12, "truncated": false},
  "duration_ms": 340,
  "error": null,
  "timestamp": "2026-09-15T12:00:00Z"
}
```

## 6. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| tasks | id, repo_path, commit, issue_text, test_cmd, max_rounds, budget, status, created_at, finished_at | 任务主表 |
| trajectory_events | id, task_id, round, state, tool, request_id, input, output_summary, duration_ms, error, timestamp | 按第 5 节格式追加 |
| patches | id, task_id, round, diff_text, changed_files, gate_result, applied | 每轮补丁与门禁结论 |
| test_runs | id, task_id, round, kind(baseline/regression/verify), passed, failed, report_path, exit_code, duration_ms | 每次测试执行 |
| evaluations | id, task_id, bug_id, localized, patch_applied, final_resolved, regression_introduced, security_blocked, rounds, tokens, duration_ms | 评测汇总,一行一任务 |

SQLite 起步,按仓储层(Repository)封装访问,预留 PostgreSQL 迁移空间。

## 7. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建任务,返回 task_id;同 repo + commit + issue 幂等 |
| GET | `/api/tasks/{task_id}` | 查询状态与进度(当前状态、轮数、耗时) |
| GET | `/api/tasks/{task_id}/trajectory` | 分页返回轨迹事件 |
| GET | `/api/tasks/{task_id}/report` | 下载最终报告(JSON/Markdown) |
| POST | `/api/tasks/{task_id}/cancel` | 取消任务,保留现场 |
| GET | `/api/health` | 健康检查 |

约束:任务执行走后台任务,不在 HTTP 请求里同步跑完;错误统一为 `{code, message, task_id}` 结构。

## 8. 目录结构(建议)

```text
patchpilot/
├── pyproject.toml
├── app/
│   ├── api/          # FastAPI 路由与 Pydantic Schema
│   ├── graph/        # LangGraph 状态、节点、条件边
│   ├── tools/        # 七个 Agent 工具
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
6. **资源门禁**:轮数、单轮时间、总 token 均在预算内。

执行隔离:每次 VERIFY 在复制出的临时工作区 + 临时容器中执行,容器加 `--network=none`、内存/CPU 限额、`--rm` 用后即删;主工作区在验证过程中保持只读。

失败处理:VERIFY 失败 → 回滚本轮补丁再进入下一轮;任务无论以何种状态结束,都保留快照、轨迹和报告。

## 10. 阶段规划

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
