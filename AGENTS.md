# AGENTS.md — PatchPilot AI 会话约定

## 个人项目快速迭代（本节优先，2026-10-12）

日常只运行根目录 `verify.ps1`（Sinan 也可 `just check`），规则见 `validation-rules.json`。
默认做改动文件静态检查及少量最近的相关测试，不自动跑全量；高风险增加相关关键链路检查。
普通 commit、push、任务/批次收尾、跨模块修改、新开对话均不构成本地全量的触发条件。
完整回归交给现有 CI；用户明确要求本地全量时才使用 `verify.ps1 -Full`。
默认最多自动选择 6 个测试文件，保留明确改动/点名的测试，其他候选记为 deferred to CI。
快速执行预算 90 秒；达到预算停止并记为未完成，不能算通过，不自动重启/扩大验证。
相同代码、配置、测试集合及环境在 24 小时内可自动复用通过收据；失败/超时/过期不复用。
AI 不得因“再确认一下”、补进度记录、收据登记而另跑全库 pytest/build/eval/coverage。
缺少测试映射时补相关手工 smoke 或点名测试并明确未验证项，不默认写新测试体系或跑全集。
新 bug 和数据/权限/费用边界补必要护栏；探索性功能先确认能用，不为每个函数补形式测试。
每轮只报告改动、实际检查结果及剩余 CI 项；部分通过不得称为全量通过。工具自动记收据。
以上只调整开发节奏，产品内质量门禁、断言、发布证据和 CI 标准保持；现有 dirty 不能覆盖。

## 项目是什么

PatchPilot:面向软件仓库的测试失败诊断与补丁验证平台。
给定本地 Git 仓库 + Bug 描述,Agent 检索代码、生成补丁、在隔离工作区执行测试,
并按显式合取判定修复结果(补丁非空 ∧ 门禁全过 ∧ 原失败测试通过 ∧ 回归测试通过
∧ 资源未超预算 ∧ 未取消;`app/graph/acceptance.py` 是唯一判定实现)。

架构与状态机定义见 `docs/` 与《PatchPilot项目企划书》;当前升级任务卡见 `docs/PatchPilot修复Agent升级SPEC-2026-10-10.md`;已执行计划与完成记录见 `docs/archive/README.md`。

## 编码规范

- Python 3.11+,全量类型标注;`ruff` 做 lint 与格式化(`ruff check . && ruff format --check .` 必须通过);
- 异常分层:`TaskError / ExecError / GateError / BudgetError / TaskCancelled`(见 `app/errors.py`;补丁应用失败是结构化 `PatchApplyResult` 而非异常,故无 PatchError),API 层兜底转统一错误结构 `{code, message, task_id}`;
- 日志:stdlib `logging`,业务日志必须带 `task_id` 与 `request_id`;
- 配置:一律走 `app/config.py` 的 `Settings`(pydantic-settings),禁止硬编码路径与密钥;
- 测试:pytest;新功能优先复用现有测试，bug/数据/权限/费用边界补必要护栏;测试不依赖网络,模型交互一律用 `app/llm/fake.py` 的 FakeLLM。

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
- 每卡提交前完成下节规定的快速检查与定向测试;全量 pytest 由 CI 执行，本地只在用户明确要求时运行。

## 开发验证节奏（个人项目）

每卡先运行 `verify.ps1`；普通卡交付、批次退出和 push 不再要求本地全集收据。
说明“本地相关检查通过，完整回归待 CI”即可继续推进；发布完整证据以最终提交 CI 为准。
`pytest -m "not slow"` 快档是可选模块排查工具，不是每卡必跑步骤。
全量 `pytest -m "not stress"` 只在用户明确要求本地全量时跑，CI 保留全集含 stress。
卡内失败先复现失败项，修复后跑相关测试；不因记账或新对话重跑。
具体默认测试选择、90 秒预算、收据复用见根目录 validation-rules.json，收据自动保存。
产品内候选 hash、失败/回归测试身份、resolved 判定与质量门禁仍按原规格执行。

### pytest 临时目录

  **basetemp 落点不许在仓库内**(`tests/conftest.py` 的 `_basetemp_outside_repo` 会当场拦红,
  成因见 PROGRESS 的 M10 卡:仓库在临时目录的父链上会让"其实不是 git 工作区"这类夹具缺陷悄悄变绿);
  而本机 `%TEMP%\pytest-of-*` 的 ACL 是坏的(PM-004,至今未修),所以**本地跑要显式传仓库外落点**:
  `pytest -q --basetemp=D:/tmp/pt`。CI 上系统临时目录可用,`pytest -q` 直接跑即可。

## 记账与文档

- CI 读数(某次 run 的通过数、连绿计数)不写进仓库文档、不为它单独提交——CI 状态以 GitHub Actions 为准;
- 进度文件(当前为 `docs/work-notes/agent-upgrade-progress-2026-10-10.md`,`PROGRESS.md` 为总日志)每张卡一段:
  做了什么、为什么、commit、测试收据。不为"订正读数 / 补读数"单独提交,有订正就并入下一个正常 commit;
- 证据类文档(`release-evidence-*`、`resume-evidence-*` 等)只在发布节点刷新,不随每次 CI 更新;
- ADR 的「验证锚点」写 `` `路径` — `期望子串` ``,不写行号(`tests/test_docs_anchors.py` 会拒绝带行号的旧写法);
  子串选能唯一说明事实的整句或定义行(如 `def foo`),不要选 `10 个` 这类会在别处碰巧出现的短串。
