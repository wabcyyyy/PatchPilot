# AGENTS.md — PatchPilot AI 会话约定

任何 AI 助手在本仓库工作前必须先读完本文件。

## 项目是什么

PatchPilot:面向软件仓库的测试失败诊断与补丁验证平台。
给定本地 Git 仓库 + Bug 描述,Agent 检索代码、生成补丁、在隔离工作区执行测试,
并按"原失败测试通过 + 回归测试通过 + 修改范围合法"判定修复结果。

架构与状态机定义见 `docs/` 与《PatchPilot项目企划书》;当前升级任务卡见 `docs/PatchPilot修复Agent升级SPEC-2026-10-10.md`;已执行计划与完成记录见 `docs/archive/README.md`。

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
- 每卡提交前完成下节规定的快速检查与定向测试;全量 pytest 在批次退出时执行。

## 开发验证节奏(2026-10-11 起生效)

本节统一规定开发本仓库时的验证频率,SPEC 与交接模板引用本节。批次沿用 SPEC 的 B0–B6;
新开对话、单卡提交、进度登记均不自动构成新的批次。独立代码任务以该任务交付作为批次退出点。
本节调整开发检查时机,产品内部的候选 hash、失败/回归测试身份、质量门禁与最终 resolved 判定仍按原规格执行。

测试分三档(登记表 `tests/slow_tests.txt`,`slow` 标记由 conftest 自动打;断言不分档):

| 时机 | 命令 | 本机耗时 |
|---|---|---|
| **每卡提交前** | `ruff check . && ruff format --check .` + 定向测试 + 快档 `pytest -q -m "not slow" --basetemp=D:/tmp/pt` | 约 2 分钟 + 定向 |
| **批次退出 / push 到 master 前**(一次) | `pytest -q -m "not stress" --basetemp=D:/tmp/pt --durations=30` | 约 20 分钟 |
| CI(最终闸) | `pytest -q`(全集,含 stress) | — |

- 开始工作:检查 cwd、HEAD、branch、dirty 文件与解释器,读取当前进度及最近测试收据。
  升级进度见 `docs/work-notes/agent-upgrade-progress-2026-10-10.md`;用户说“继续 SPEC/当前批次”时,
  据此定位未完成卡或下一批,不默认从 B0 重启。
  不因新开对话自动重跑基线或全量;先核对旧收据的被测文件、配置、依赖与环境是否适用。
  旧结果只能证明其记录的输入,不能直接声明后续代码已经全量通过。
- 卡内调试:新增必要行为测试,只跑新功能与直接受影响的回归测试。测试集合根据修改点与调用链选择
  (改动触及图/执行器/服务主链路时,"受影响"包括会跑到这段代码的端到端测试文件);
  重复或扩大范围前,说明具体代码变化、失败或未解决风险,不以“再确认一下”为理由重跑。
- 新用例单条超过 2s 的登记进 `tests/slow_tests.txt`(跑完的摘要会点名超过 5s 却未登记的用例);
  新功能优先在节点/函数层测,端到端用例每张卡 1–2 条为宜,能共用一次图运行的用 module 级夹具共用。
- 每卡提交前:见上表。修改 ADR 锚定的源码/文档时顺带跑 `tests/test_docs_anchors.py`(快档已含)。
  检查已覆盖最终待提交输入且之后仅更新不影响检查的进度记录时,复用该收据。
- 批次退出:所有卡及相关文档准备好后,运行一次本地全量(见上表),`--durations=30` 读数用于维护慢用例登记表。
  已有适用的 Ruff/格式/锚点结果可复用。缺全量通过收据时,卡内进度标“定向通过,批次全量待运行”;
  不宣称批次完成,不以测试数量对账代替通过结果。
- 失败处理:先单独复现失败项;修复后跑失败项及受影响测试,通过后再做必要的批次最终全量。
  全量通过后只有进度文字/收据登记变化时,运行受影响文档检查即可;业务代码、测试、执行配置或依赖变化
  则需要重新完成适用的定向检查和批次最终全量。产品失败、环境失败、未运行分开记录。
- 纯文档任务(包括本验证约定的维护):检查 diff、链接与规则一致性,按影响运行文档锚点测试;
  未改变 Python 文件、执行配置或依赖时,不运行全量 pytest 或重复 Ruff。
- 同一工作树保持 pytest 串行;全量期间不修改被测源码、测试、配置与锚点文档,也不另起 pytest。
  有限轮询已启动进程,保留日志与终态退出码;缺进度时先查看进程和日志,不重复启动同一命令。
- 跨对话收据至少记录:被测 HEAD/dirty 文件、命令/测试集合、解释器与依赖身份、basetemp、退出码、
  耗时与日志路径、通过/失败/skip、全量状态。测试收据与下一卡写入当前进度文件;
  中途换对话继续该批次,不为重新证明已完成卡而从头验证。

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
