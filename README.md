# PatchPilot

**面向软件仓库的测试失败诊断与补丁验证平台**。

给定一个本地 Git 仓库和一段 Bug 描述,PatchPilot Agent 自动检索相关代码、生成补丁、
在隔离工作区执行测试,并按显式判定规则给出结论:

> `resolved` = 补丁非空 **且** 原失败测试全部转通过 **且** 回归测试全部保持通过 **且** 补丁通过全部质量门禁 **且** 未超资源预算 **且** 未取消。graph 还核对冻结候选与当前工作区一致；开启双跑时须两次一致。

参考 SWE-agent 的工具循环设计与 SWE-bench 的评测判定思路,独立实现的工程子集;
不声称复刻完整 SWE-agent,不承诺 SWE-bench 跑分。

## 功能概览

- **LangGraph 状态机**:CREATED → BASELINE → LOCALIZE → PLAN → PROPOSE_PATCH → APPLY_PATCH → VERIFY → FINISHED,
  外加 INVALID_TASK / PATCH_REJECTED / VERIFY_FAILED / BUDGET_EXCEEDED / NEEDS_REVIEW 异常分支;
- **16 个受控 Agent 工具**:list_files / search_code / find_symbol / describe_file / read_file / read_evidence / apply_patch / run_tests / write_repro / run_command / git_diff / reset_workspace / update_memory / propose_acceptance / ask_user / delegate,
  每次调用记录完整轨迹(JSONL + SQLite);
- **七项质量门禁**:格式 / 禁改测试文件 / 路径越界 / 修改范围 / 影子模块 / 命令白名单 / 资源预算,
  附 15 个攻击样例(`bugs/attacks/`,含初始 10 个攻击样例门禁样例与 5 个对抗评测样例)验证拦截;
- **双执行引擎**:plain 单轮驱动与 graph 分阶段、多候选状态机，共享模型工具循环、工具、pytest 适配器与终局验收；候选工件、审批/澄清与自动恢复编排在 graph 路径。两者不声明功能等价;
- **服务化**:FastAPI 19 个端点(`app/api/routes.py`,条数由 `tests/test_docs_anchors.py` 对路由装饰器核数) + SQLite 持久化 + Redis 任务锁(可退化内存锁)+ 幂等 + 崩溃恢复;
- **容器执行后端（默认 local）**:pytest 临时容器带 `--network=none`、内存/CPU 限额、`--rm`；受控诊断命令另加 PID 限额与 `no-new-privileges`，
  5 项隔离实验见 `docs/archive/docker-isolation-notes.md`;
- **自建评测集**:正式集 35 题(28 题简单/中等 + 7 题 hard,BUG-029..035),
  另有候选 1 道待审(`bugs/candidates/`),
  七项指标自动判定;T10.1 起报告自带批次 provenance 与自动归并的复现命令
  (已入库的两份报告快照早于该特性,读数前先看 `docs/README.md` 的口径补注);
- **可信验收链(2026-10-08 spec)**:任务契约哈希幂等、完整测试身份匹配(类链/参数化)、
  受理输入冻结副本 + 环境预检、崩溃后按候选补丁重应用并完整重验、
  任务级资源账本与共享终局验收(`docs/adr/0009`、`docs/adr/0010`);
- **证据可取回 + 跨阶段传递(2026-10-11 升级 SPEC G05/G06)**:工具回执在**序列化之前**按数据结构裁剪
  (发出的消息恒为合法 JSON,并标注省略行数与保留的原始行号区间),原文落 `<run_dir>/evidence/`,
  `read_evidence` 按钉在工件上的游标分页取回;任务记忆由平台从真实回执自动登记观测,
  模型只能提交带引用的假设(`update_memory`,写"已验证"会被点名拒绝),
  阶段之间带过去的是这些观测而非模型自述;恢复快照绑定会话/记忆 hash,不一致即待审
  (`docs/adr/0012`、`docs/adr/0013`);
- **容量按声明走(2026-10-11 升级 SPEC G07)**:模型能力只能显式声明并带来源
  (`operator_config`/`provider_docs`/`probed`),没来源就落保守档且**申报更大一律拒绝发起**
  —— 不从模型名推断窗口;单请求硬容量、软压缩目标、任务累计额度、单次输出上限四件事
  各有自己的名字与读数,`legacy_v1`(= 今日逐字)与 `repair_v2`(目标 64k/尾部钉 12,
  只在能力允许时采用,被夹过写进 TaskSpec)按任务冻结成一份 profile 实例传入两引擎;
  压不动的保护区超出可用余量时给 `context_capacity_exceeded` 而不是反复压缩空转,
  端点回报的 `served_model` 与请求名不一致就标漂移(`docs/adr/0014`);
- **仓库规则进请求,但不进权限面(2026-10-11 升级 SPEC G08)**:被诊断仓库自带的 `AGENTS.md` /
  `CLAUDE.md` 从**冻结输入副本**按固定文件名加载(同目录只取最高优先级那份并明写落选原因),
  根说明开局就在、目录说明要等平台真实观测到该目录才进场;超限一律明写
  (单文件 16KiB 截断 + "另有 N 字节未显示"、总量 64KiB 整份不纳入并点名路径),
  符号链接/`.git`/二进制/宿主路径一律拒绝,形似凭据的行渲染前打码而身份仍按原始字节。
  优先级固定为平台硬规则 > 用户任务约束 > 仓库说明,**T13**:规则只指导怎么改,
  工具白名单、门禁与验收契约的判定路径完全不读这一层(`docs/adr/0015`);
- **指标与消融**:评测指标带版本(空补丁不再计入严格定位),同引擎两臂消融策略;
  比较器把**控制身份**(配置快照、端点/价目/容器/协议指纹、任务与源码身份)与
  **观测结果**(verdict/token/轮数/耗时/费用)分开——结果不同合法,配置漂移即报错,
  同题重复按 `(task, arm, repetition)` 一条不折;批定义由可 hash 的实验 manifest 承载
  (`docs/adr/0011`,模板 `docs/work-notes/experiment-manifest-template-2026-10-11.json`);
- **离线收口与产品面(2026-10-12 升级 SPEC G16)**:能力查询面 `GET /api/capabilities`
  (engine/profile/arm/role/model/预算的真实有效值,与受理层同源,one_shot 如实登记为
  评测 CLI 专用);请求可显式选运行 profile(`agent_profile`,新旧客户端靠显式字段区分,
  不靠环境变量猜),MVP CLI 建任务入口 `python -m app.agent.cli submit` 新任务默认
  repair_v2;执行与恢复一律从**受理时冻结的 TaskSpec** 重建有效策略(同名 profile 数值
  漂移不改变已受理任务口径,快照绑定比 profile 指纹而不只比名字);report.json v2 补齐
  验收清单/候选/命令/输入事件/运行身份,Markdown 把三态显式呈现为"已验证/未运行/不确定";
  六个离线演示覆盖 issue-only 全流程/澄清纠偏/子任务委派/崩溃恢复(`demo/README.md`),
  离线场景对齐表见 `docs/evidence/agent-upgrade/offline-scenarios.md`,发布收据见
  `docs/evidence/agent-upgrade/release-evidence-2026-10-12.md`;
- **运行中进度**:轨迹事件实时入 SQLite,任务行带独立 stage/last_event_at 进度列
  (终态不可被迟到事件复活);
- **外部基准接入(v2)**:SWE-bench Verified 元数据 → 本地任务(`app/evals/swebench.py` +
  `scripts/import_swebench.py`),每题可自带执行环境(`env: python/image/workdir/network`,
  用官方评测镜像),复用**同一套**基线/验证/门禁判定;导入期从镜像回捞构建产物。
  真实模型批次、双跑复核一致性(8/8 mismatch=null)与"有循环 vs 无执行反馈"的两臂消融
  对照均已实跑;预登记驱动的噪声地板批(7 题 × 3 次)与负结果停手见
  `docs/archive/paid-batch-preregistration-2026-10-08.md`,口径、负面结果与瓶颈定位
  见 `docs/work-notes/swe-ablation-evidence-2026-10-07.md`。**不声称 SWE-bench 官方跑分**:样本、
  判定规则与提交形态均为自定,任何解决率数字都必须带着该文第 2 节的局限一起读。

## 快速开始

```bash
# Python 3.11+;依赖声明在 pyproject.toml,锁定在 uv.lock
# (uv sync 自动创建 .venv 并安装含 dev 组的全部依赖;下述命令也可用 uv run 前缀执行)
uv sync

# 日常快速验证（改动文件静态检查 + 少量相关测试；90 秒预算；收据自动保存）
powershell -NoProfile -ExecutionPolicy Bypass -File verify.ps1
# 本地全量仅在明确要求时：./verify.ps1 -Full；完整回归默认交 CI。
# pytest 临时目录由验证入口放在仓库外，详见 AGENTS.md「pytest 临时目录」。

# 回放模式跑一个完整修复任务(验证平台闭环)
uv run python -m app.evals.run_single --bug BUG-001 --model fake --engine graph --out runs

# 建任务入口(MVP CLI;新任务默认 profile=repair_v2,显式随请求发送;
# 老口径用 --profile legacy_v1 显式退回)
uv run python -m app.agent.cli submit --bug-id BUG-001
uv run python -m app.agent.cli submit --repo-path <path> --issue "缺陷描述"

# 服务启动后查询可受理能力(engine/profile/arm/role/model/预算,与受理层同源)
curl -s http://127.0.0.1:8001/api/capabilities

# 六个离线演示(全流程/澄清纠偏/委派/崩溃恢复等),分工表见 demo/README.md
uv run python -m demo.run_issue_only_ticket --out runs/demo-issue-only

# 批量评测 + 生成报告
uv run python -m app.evals.report --runs runs/m9 --out docs/work-notes/eval-report.md
```

接入真实模型:复制 `.env.example` 为 `.env`,配置 OpenAI 兼容端点,并**显式设置
`PATCHPILOT_LLM_ENABLED=true`**(总开关,默认关闭以防误配 key 即产生花费)后,
才允许 `--model openai` / API `model="openai"`。每次调用受
`PATCHPILOT_LLM_MAX_TOKENS`、`PATCHPILOT_LLM_TIMEOUT_SECONDS` 与
`PATCHPILOT_TOKEN_BUDGET`(单任务累计 token 预算)约束。

**DeepSeek 系端点额外建议设 `PATCHPILOT_LLM_THINKING=disabled`**(2026-10-09 实测):
这类端点的思考模式会返回 `reasoning_content`,且其"回传校验"是**间歇性**的——同一请求
先 400 后 200,无法从客户端形状规避;`disabled` 下端点每轮不返回思维链,校验无从触发
(诊断链与探针:docs/archive/paid-batch-preregistration-2026-10-08.md 补记 A、
docs/evidence/probe-thinking-400/)。付费跑批前建议先用单题冒烟确认端点条件。

## Docker Compose

```bash
cd docker
docker compose up -d --build     # api(8001) + redis
curl http://localhost:8001/api/health
curl -X POST http://localhost:8001/api/tasks -H "Content-Type: application/json" \
     -d '{"bug_id":"BUG-006","engine":"graph","model":"fake"}'
```

## 目录结构

```text
app/
├── api/        # FastAPI 路由、任务服务、报告渲染
├── graph/      # LangGraph 状态、节点、门禁、checkpoint、plain 循环
├── tools/      # 16 个受控 Agent 工具、轨迹记录、路径安全
├── executor/   # 本地 / Docker 测试执行器、命令白名单
├── gitops/     # 快照、diff、应用补丁、回滚
├── adapters/   # pytest 报告解析(预留其他语言)
├── llm/        # 模型封装(OpenAI 兼容 + FakeLLM 回放)
├── storage/    # SQLite 仓储 + Redis/内存任务锁
└── evals/      # 题目加载、单任务驱动、指标计算、报告生成
bugs/           # 自建 Bug 任务集 + 攻击样例 + hard 候选(计数以 bugs/README.md 与 list_bug_ids 为准)
docker/         # 执行器镜像、API 镜像、Compose
docs/           # 长期规范、当前 SPEC；阶段材料 work-notes/、adr/、evidence/、archive/
tests/          # 项目自身测试(全离线,模型交互用 FakeLLM 回放;用例数不手写,以本地可复现实测为准:PATCHPILOT_LLM_ENABLED=false python -m pytest -q --basetemp=D:/tmp/pt)
```

## 文档

**从 [`docs/README.md`](docs/README.md) 进**——那份索引规定了每类问题由哪个文件唯一回答。

| 文档 | 内容 |
|---|---|
| `docs/README.md` | **docs 索引**:唯一事实源、文件状态、维护约定 |
| `docs/runbook.md` | **运维值守与应急手册**: 生产架构、启停顺序、健康探针、SOP 与平滑回滚 |
| `docs/pilot-release-evidence.md` | **受控生产试点发布证据**: 准入矩阵、评测基线、已知限制与风险说明 |
| `docs/eval-commercial-baseline-guide.md` | **商业模型评测基线指南**: 商业大模型接入规范、预算封包、急停护栏与台账审计 |
| `docs/work-notes/p21-multi-instance-spec.md` | **P21 多实例多租户规格**: 分布式作业队列、PostgreSQL/对象存储迁移与双活容灾蓝图 |
| `docs/PatchPilot修复Agent升级SPEC-2026-10-10.md` | 当前升级规格:分批任务与验收 |
| `docs/work-notes/README.md` | 阶段材料索引:审查、交接、报告和探针 |
| `docs/PatchPilot项目企划书.md` | 项目范围与当前架构概要；早期规划另标历史，升级行为以当前 SPEC/ADR 为准 |
| `docs/work-notes/realignment-2026-10-12.md` | 本轮真实链路审查、遗留分类、修复范围及验证收据 |
| `docs/interview-guide/README.md` | 对应当前实现的面试材料及证据边界 |
| `docs/archive/design.md` | 设计决策与已知边界(已归档,仍被 ADR 锚点引用) |
| `docs/threat-model.md` | 威胁模型:风险清单、缓解与"明确不防"清单 |
| `docs/archive/audit-2026-09-19.md` | 全量自检:死代码/冗余架构、安全与逻辑漏洞、承诺落地核查,按 P0–P3 分级 |
| `docs/work-notes/eval-report.md` | fake 回放批次快照(混合引擎;读数前先读文首口径补注) |
| `docs/work-notes/eval-report-real.md` | 真实模型批次快照(缺模型名/成本;读数前先读文首口径补注) |
| `docs/work-notes/swe-ablation-evidence-2026-10-07.md` | 真实 SWE-bench 批次 + 两臂消融对照:预登记题单与判据、负面结果、瓶颈定位与协议边界 |
| `docs/work-notes/interview-evidence-2026-10-07.md` | 同一批实测的"能写 / 不能写"清单:每条主张附证据路径与强度上限 |
| `docs/postmortems/` | 失败复盘(6 篇) |
| `docs/archive/docker-isolation-notes.md` / `docs/archive/docker-backend-notes.md` | 容器隔离边界实验 / 执行后端与部署闭环 |
| `docs/archive/README.md` | 历史计划、执行状态与收口记录 |
| `docs/archive/PatchPilot学习计划.md` | 学习路线 |
| `docs/archive/` | 一次性历史:夜跑任务单与晨会报告、M10–M13 改进计划(均已完成) |
