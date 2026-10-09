# PatchPilot

**面向软件仓库的测试失败诊断与补丁验证平台**。

给定一个本地 Git 仓库和一段 Bug 描述,PatchPilot Agent 自动检索相关代码、生成补丁、
在隔离工作区执行测试,并按显式判定规则给出结论:

> `resolved` = 原失败测试全部转通过 **且** 回归测试全部保持通过 **且** 补丁通过全部质量门禁 **且** 未超资源预算。

参考 SWE-agent 的工具循环设计与 SWE-bench 的评测判定思路,独立实现的工程子集;
不声称复刻完整 SWE-agent,不承诺 SWE-bench 跑分。

## 功能概览

- **LangGraph 状态机**:CREATED → BASELINE → LOCALIZE → PLAN → PROPOSE_PATCH → APPLY_PATCH → VERIFY → FINISHED,
  外加 INVALID_TASK / PATCH_REJECTED / VERIFY_FAILED / BUDGET_EXCEEDED / NEEDS_REVIEW 异常分支;
- **9 个受控 Agent 工具**:list_files / search_code / find_symbol / describe_file / read_file / apply_patch / run_tests / git_diff / reset_workspace,
  每次调用记录完整轨迹(JSONL + SQLite);
- **七项质量门禁**:格式 / 禁改测试文件 / 路径越界 / 修改范围 / 影子模块 / 命令白名单 / 资源预算,
  附 10 个攻击样例(`bugs/attacks/`)验证拦截;
- **双执行引擎**:plain(教学用单循环)与 graph(状态机),共享工具与提示;
- **服务化**:FastAPI 七个端点 + SQLite 持久化 + Redis 任务锁(可退化内存锁)+ 幂等 + 崩溃恢复;
- **容器隔离**:临时容器执行测试(`--network=none`、内存/CPU 限额、`--rm`),
  5 项隔离实验见 `docs/docker-isolation-notes.md`;
- **自建评测集**:正式集 35 题(28 题简单/中等 + 7 题 hard,BUG-029..035),
  另有候选 1 道待审(`bugs/candidates/`),
  七项指标自动判定;T10.1 起报告自带批次 provenance 与自动归并的复现命令
  (已入库的两份报告快照早于该特性,读数前先看 `docs/README.md` 的口径补注);
- **可信验收链(2026-10-08 spec)**:任务契约哈希幂等、完整测试身份匹配(类链/参数化)、
  受理输入冻结副本 + 环境预检、崩溃后按候选补丁重应用并完整重验、
  任务级资源账本与共享终局验收(`docs/adr/0009`、`docs/adr/0010`);
- **运行中进度**:轨迹事件实时入 SQLite,任务行带独立 stage/last_event_at 进度列
  (终态不可被迟到事件复活);
- **指标与消融**:评测指标带版本(空补丁不再计入严格定位),同引擎两臂消融策略
  (未登记差异的比较器直接报错,`docs/experiment-preregistration-v2-2026-10-08.md`);
- **外部基准接入(v2)**:SWE-bench Verified 元数据 → 本地任务(`app/evals/swebench.py` +
  `scripts/import_swebench.py`),每题可自带执行环境(`env: python/image/workdir/network`,
  用官方评测镜像),复用**同一套**基线/验证/门禁判定;导入期从镜像回捞构建产物。
  真实模型批次、双跑复核一致性(8/8 mismatch=null)与"有循环 vs 无执行反馈"的两臂消融
  对照均已实跑;预登记驱动的噪声地板批(7 题 × 3 次)与负结果停手见
  `docs/paid-batch-preregistration-2026-10-08.md`,口径、负面结果与瓶颈定位
  见 `docs/swe-ablation-evidence-2026-10-07.md`。**不声称 SWE-bench 官方跑分**:样本、
  判定规则与提交形态均为自定,任何解决率数字都必须带着该文第 2 节的局限一起读。

## 快速开始

```bash
# Python 3.11+
python -m pip install -r requirements.txt -r requirements-dev.txt

# 运行测试(全离线,模型交互用 FakeLLM 回放)
# 临时根落点不许在仓库内(防线见 tests/conftest.py);Windows 本机系统临时目录的
# pytest 根 ACL 已坏(PM-004),本地要显式传仓库外落点,CI 用默认值即可
pytest -q --basetemp=D:/tmp/pt

# 回放模式跑一个完整修复任务(验证平台闭环)
python -m app.evals.run_single --bug BUG-001 --model fake --engine graph --out runs

# 批量评测 + 生成报告
python -m app.evals.report --runs runs/m9 --out docs/eval-report.md
```

接入真实模型:复制 `.env.example` 为 `.env`,配置 OpenAI 兼容端点,并**显式设置
`PATCHPILOT_LLM_ENABLED=true`**(总开关,默认关闭以防误配 key 即产生花费)后,
才允许 `--model openai` / API `model="openai"`。每次调用受
`PATCHPILOT_LLM_MAX_TOKENS`、`PATCHPILOT_LLM_TIMEOUT_SECONDS` 与
`PATCHPILOT_TOKEN_BUDGET`(单任务累计 token 预算)约束。

**DeepSeek 系端点额外建议设 `PATCHPILOT_LLM_THINKING=disabled`**(2026-10-09 实测):
这类端点的思考模式会返回 `reasoning_content`,且其"回传校验"是**间歇性**的——同一请求
先 400 后 200,无法从客户端形状规避;`disabled` 下端点每轮不返回思维链,校验无从触发
(诊断链与探针:docs/paid-batch-preregistration-2026-10-08.md 补记 A、
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
├── tools/      # 七个 Agent 工具、轨迹记录、路径安全
├── executor/   # 本地 / Docker 测试执行器、命令白名单
├── gitops/     # 快照、diff、应用补丁、回滚
├── adapters/   # pytest 报告解析(预留其他语言)
├── llm/        # 模型封装(OpenAI 兼容 + FakeLLM 回放)
├── storage/    # SQLite 仓储 + Redis/内存任务锁
└── evals/      # 题目加载、单任务驱动、指标计算、报告生成
bugs/           # 自建 Bug 任务集 + 攻击样例 + hard 候选(计数以 bugs/README.md 与 list_bug_ids 为准)
docker/         # 执行器镜像、API 镜像、Compose
docs/           # 索引(docs/README.md)、设计笔记、威胁模型、隔离实验、复盘、审计、adr/、archive/
tests/          # 项目自身测试(全离线,模型交互用 FakeLLM 回放;用例数不手写,以本地可复现实测为准:PATCHPILOT_LLM_ENABLED=false python -m pytest -q --basetemp=D:/tmp/pt)
```

## 文档

**从 [`docs/README.md`](docs/README.md) 进**——那份索引规定了每类问题由哪个文件唯一回答。

| 文档 | 内容 |
|---|---|
| `docs/README.md` | **docs 索引**:唯一事实源、文件状态、维护约定 |
| `docs/PatchPilot项目企划书.md` | 规范源:架构、状态机、门禁、判定规则、评测方案 |
| `docs/design.md` | 设计决策与已知边界 |
| `docs/threat-model.md` | 威胁模型:风险清单、缓解与"明确不防"清单 |
| `docs/audit-2026-09-19.md` | 全量自检:死代码/冗余架构、安全与逻辑漏洞、承诺落地核查,按 P0–P3 分级 |
| `docs/eval-report.md` | fake 回放批次快照(混合引擎;读数前先读文首口径补注) |
| `docs/eval-report-real.md` | 真实模型批次快照(缺模型名/成本;读数前先读文首口径补注) |
| `docs/swe-ablation-evidence-2026-10-07.md` | 真实 SWE-bench 批次 + 两臂消融对照:预登记题单与判据、负面结果、瓶颈定位与协议边界 |
| `docs/interview-evidence-2026-10-07.md` | 同一批实测的"能写 / 不能写"清单:每条主张附证据路径与强度上限 |
| `docs/postmortems/` | 失败复盘(6 篇) |
| `docs/docker-isolation-notes.md` / `docs/docker-backend-notes.md` | 容器隔离边界实验 / 执行后端与部署闭环 |
| 《PatchPilot开发计划书.md》 / 《PatchPilot学习计划.md》 | 里程碑任务卡 / 学习路线 |
| `docs/archive/` | 一次性历史:夜跑任务单与晨会报告、M10–M13 改进计划(均已完成) |
