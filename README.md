# PatchPilot

**面向软件仓库的测试失败诊断与补丁验证平台**。

给定一个本地 Git 仓库和一段 Bug 描述,PatchPilot Agent 自动检索相关代码、生成补丁、
在隔离工作区执行测试,并按显式判定规则给出结论:

> `resolved` = 原失败测试全部转通过 **且** 回归测试全部保持通过 **且** 补丁通过全部质量门禁 **且** 未超资源预算。

参考 SWE-agent 的工具循环设计与 SWE-bench 的评测判定思路,独立实现的工程子集;
不声称复刻完整 SWE-agent,不承诺 SWE-bench 跑分。

## 文档

| 文档 | 内容 |
|---|---|
| `docs/设计笔记` 与《PatchPilot项目企划书.md》 | 架构、状态机、门禁、评测方案 |
| 《PatchPilot开发计划书.md》 | 里程碑 M0—M9 与任务卡 |
| 《PatchPilot学习计划.md》 | 配套学习路线 |

## 快速开始

```bash
# Python 3.11+
python -m pip install -r requirements.txt -r requirements-dev.txt

# 运行测试
pytest -q

# 用回放模式跑一个完整修复任务(离线、确定性,验证平台闭环)
python -m app.evals.run_single --bug bugs/BUG-001 --model fake --out runs
```

接入真实模型:复制 `.env.example` 为 `.env`,配置 OpenAI 兼容端点后 `--model openai`。

## 目录结构

```text
app/
├── api/        # FastAPI 路由与 Schema
├── graph/      # LangGraph 状态、节点、条件边、门禁
├── tools/      # 七个 Agent 工具与轨迹记录
├── executor/   # 本地 / Docker 测试执行
├── gitops/     # 快照、diff、应用补丁、回滚
├── adapters/   # pytest 报告解析(预留其他语言)
├── llm/        # 模型封装(真实 SDK + FakeLLM 回放)
├── storage/    # SQLite / Redis 访问层
└── evals/      # 单任务驱动、指标计算、报告生成
bugs/           # 自建 Bug 任务集与攻击案例
docker/         # 执行器镜像与 Compose
docs/           # 设计笔记、复盘、评测报告
tests/          # 项目自身测试
```
