# PatchPilot

**面向软件仓库的测试失败诊断与补丁验证平台**。

给定一个本地 Git 仓库和一段 Bug 描述,PatchPilot Agent 自动检索相关代码、生成补丁、
在隔离工作区执行测试,并按显式判定规则给出结论:

> `resolved` = 原失败测试全部转通过 **且** 回归测试全部保持通过 **且** 补丁通过全部质量门禁 **且** 未超资源预算。

参考 SWE-agent 的工具循环设计与 SWE-bench 的评测判定思路,独立实现的工程子集;
不声称复刻完整 SWE-agent,不承诺 SWE-bench 跑分。

## 功能概览

- **LangGraph 状态机**:CREATED → BASELINE → LOCALIZE → PROPOSE_PATCH → APPLY_PATCH → VERIFY → FINISHED,
  外加 INVALID_TASK / PATCH_REJECTED / VERIFY_FAILED / BUDGET_EXCEEDED / NEEDS_REVIEW 异常分支;
- **7 个受控 Agent 工具**:list_files / search_code / read_file / apply_patch / run_tests / git_diff / reset_workspace,
  每次调用记录完整轨迹(JSONL + SQLite);
- **六项质量门禁**:格式 / 禁改测试文件 / 路径越界 / 修改范围 / 命令白名单 / 资源预算,
  附 4 个攻击样例(`bugs/attacks/`)验证拦截;
- **双执行引擎**:plain(教学用单循环)与 graph(状态机),共享工具与提示;
- **服务化**:FastAPI 六端点 + SQLite 持久化 + Redis 任务锁(可退化内存锁)+ 幂等 + 崩溃恢复;
- **容器隔离**:临时容器执行测试(`--network=none`、内存/CPU 限额、`--rm`),
  5 项隔离实验见 `docs/docker-isolation-notes.md`;
- **自建评测集**:20 道 Python Bug(异常处理/边界条件/类型错误/数据访问/跨文件定位 × 简单/中等),
  七项指标自动判定,报告一键复现。

## 快速开始

```bash
# Python 3.11+
python -m pip install -r requirements.txt -r requirements-dev.txt

# 运行测试(全离线,模型交互用 FakeLLM 回放)
pytest -q

# 回放模式跑一个完整修复任务(验证平台闭环)
python -m app.evals.run_single --bug BUG-001 --model fake --engine graph --out runs

# 批量评测 + 生成报告
python -m app.evals.report --runs runs/m9 --out docs/eval-report.md
```

接入真实模型:复制 `.env.example` 为 `.env`,配置 OpenAI 兼容端点后 `--model openai`。

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
bugs/           # 20 道自建 Bug + 4 个攻击样例(scripts/gen_bugs.py 生成)
docker/         # 执行器镜像、API 镜像、Compose
docs/           # 设计笔记、隔离实验、复盘(PM-001~006)、评测报告
tests/          # 项目自身测试(115+ 用例)
```

## 文档

| 文档 | 内容 |
|---|---|
| `docs/design.md` | 设计决策与已知边界 |
| `docs/eval-report.md` | 最新评测报告(可复现) |
| `docs/postmortems/` | 失败复盘(6 篇) |
| `docs/docker-isolation-notes.md` | 容器隔离边界实验 |
| 《PatchPilot项目企划书.md》 | 架构、状态机、门禁、评测方案 |
| 《PatchPilot开发计划书.md》 / 《PatchPilot学习计划.md》 | 里程碑任务卡 / 学习路线 |
