# AGENTS.md — PatchPilot AI 会话约定

任何 AI 助手在本仓库工作前必须先读完本文件。

## 项目是什么

PatchPilot:面向软件仓库的测试失败诊断与补丁验证平台。
给定本地 Git 仓库 + Bug 描述,Agent 检索代码、生成补丁、在隔离工作区执行测试,
并按"原失败测试通过 + 回归测试通过 + 修改范围合法"判定修复结果。

架构与状态机定义见 `docs/` 与《PatchPilot项目企划书》;当前开发任务卡见《PatchPilot开发计划书》。

## 编码规范

- Python 3.11+,全量类型标注;`ruff` 做 lint 与格式化(`ruff check . && ruff format --check .` 必须通过);
- 异常分层:`TaskError / PatchError / ExecError / GateError / BudgetError`(见 `app/errors.py`),API 层兜底转统一错误结构 `{code, message, task_id}`;
- 日志:stdlib `logging`,业务日志必须带 `task_id` 与 `request_id`;
- 配置:一律走 `app/config.py` 的 `Settings`(pydantic-settings),禁止硬编码路径与密钥;
- 测试:pytest;新功能必须带测试;测试不依赖网络,模型交互一律用 `app/llm/fake.py` 的 FakeLLM。

## 禁区

- 禁止修改测试的期望行为来让测试通过;
- 禁止改动质量门禁规则来放行本应拦截的输入;
- 禁止在 `app/graph/`、`app/gitops/`、`app/executor/local_runner.py`、`app/tools/` 的边界校验、
  `app/adapters/pytest_adapter.py` 的签名规则、`app/evals/metrics.py` 的判定逻辑中引入未经讨论的语义变更——
  这些是"必须掌握"区,AI 只能按规格实现或 review;
- 单次生成不超过 300 行;只修改当前任务卡声明范围内的文件;
- 不复制外部项目大段代码,只借鉴设计与接口。

## 提交规范

- Conventional Commits:`feat(scope): ...`、`fix(scope): ...`、`test(scope): ...`、`docs(scope): ...`;
- 每张任务卡至少一个 commit;
- 提交前本地必须跑绿:`ruff check . && pytest -q`。
