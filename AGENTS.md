# AGENTS.md — PatchPilot AI 会话约定

任何 AI 助手在本仓库工作前必须先读完本文件。

## 项目是什么

PatchPilot:面向软件仓库的测试失败诊断与补丁验证平台。
给定本地 Git 仓库 + Bug 描述,Agent 检索代码、生成补丁、在隔离工作区执行测试,
并按"原失败测试通过 + 回归测试通过 + 修改范围合法"判定修复结果。

架构与状态机定义见 `docs/` 与《PatchPilot项目企划书》;当前开发任务卡见《PatchPilot开发计划书》。

## 编码规范

- Python 3.11+,全量类型标注;`ruff` 做 lint 与格式化(`ruff check . && ruff format --check .` 必须通过);
- 异常分层:`TaskError / ExecError / GateError / BudgetError / TaskCancelled`(见 `app/errors.py`;补丁应用失败是结构化 `PatchApplyResult` 而非异常,故无 PatchError),API 层兜底转统一错误结构 `{code, message, task_id}`;
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
- 测试分三档(登记表 `tests/slow_tests.txt`,`slow` 标记由 conftest 自动打;断言不分档):

  | 时机 | 命令 | 本机耗时 |
  |---|---|---|
  | **每个 commit 前** | `ruff check . && ruff format --check .` + 受影响的测试文件 + 快档 `pytest -q -m "not slow" --basetemp=D:/tmp/pt` | 约 2 分钟 |
  | **每张卡收尾 / push 前**(一次) | `pytest -q -m "not stress" --basetemp=D:/tmp/pt` | 约 20 分钟 |
  | CI(最终闸) | `pytest -q`(全集,含 stress) | — |

  不要每个 commit 都跑全量;卡内中间 commit 只需快档 + 受影响文件。改动若触及图/执行器/服务主链路,
  受影响文件按"会跑到这段代码的端到端测试文件"算,不只是同名测试文件。
- 新用例单条超过 2s 的,登记进 `tests/slow_tests.txt`(跑完的摘要会点名超过 5s 却未登记的用例);
  新功能优先在节点/函数层测,端到端用例每张卡 1–2 条为宜,能共用一次图运行的用 module 级夹具共用。
- **basetemp 落点不许在仓库内**(`tests/conftest.py` 的 `_basetemp_outside_repo` 会当场拦红,
  成因见 PROGRESS 的 M10 卡:仓库在临时目录的父链上会让"其实不是 git 工作区"这类夹具缺陷悄悄变绿);
  而本机 `%TEMP%\pytest-of-*` 的 ACL 是坏的(PM-004,至今未修),所以**本地跑要显式传仓库外落点**:
  `pytest -q --basetemp=D:/tmp/pt`。CI 上系统临时目录可用,`pytest -q` 直接跑即可。

## 记账与文档

- CI 读数(某次 run 的通过数、连绿计数)不写进仓库文档、不为它单独提交——CI 状态以 GitHub Actions 为准;
- `PROGRESS.md` 每张卡一段:做了什么、为什么、commit、收尾全量结果一行。不为"订正读数 / 补读数"单独提交,
  有订正就并入下一个正常 commit;
- 证据类文档(`docs/release-evidence-*`、`resume-evidence-*` 等)只在发布节点刷新,不随每次 CI 更新;
- ADR 的「验证锚点」写 `` `路径` — `期望子串` ``,不写行号(`tests/test_docs_anchors.py` 会拒绝带行号的旧写法);
  子串选能唯一说明事实的整句或定义行(如 `def foo`),不要选 `10 个` 这类会在别处碰巧出现的短串。
