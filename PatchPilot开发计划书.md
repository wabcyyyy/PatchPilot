# PatchPilot 开发计划书(vibe-coding 版)

> 本文是《PatchPilot项目企划书》的执行层文档:把五个阶段拆成 10 个里程碑、40+ 张任务卡,每张卡标注**产出文件、依赖、AI 参与度、验收命令**。
> 原则是"验收先行、小步提交":每张卡开工前先明确验收命令,收工的唯一标准是命令跑绿 + 能复述关键逻辑。
> 学习节奏对照《PatchPilot学习计划》第 7 节周表;架构、状态机、门禁规则以企划书为准。

## 1. Vibe-coding 开发原则

1. **验收先行**:每张任务卡先写"验收命令 + 期望输出",再让 AI 动手;没有验收标准的卡不许开工。
2. **小步快跑**:单张卡工作量 ≤ 半天;单次让 AI 生成 ≤ 300 行,超了就拆卡。
3. **边界清单驱动**:每个目录有明确的主导方(自己 / AI),见第 2 节地图;"必须掌握"区 AI 只能 review,不能代写。
4. **三步验收**:AI 产出后依次执行——① 跑验收命令;② 通读 diff;③"必须掌握"区的代码逐行复述作用,复述不出就退回。
5. **一切落盘**:trajectory、失败复盘、评测报告全部写进仓库,聊天记录不算产出。
6. **防跑偏规则**:每张卡只允许改卡内声明的文件;AI 顺手"优化"范围外的代码一律 reject。

## 2. AI 参与度地图

| 路径 | 主导方 | 理由 |
|---|---|---|
| `app/graph/`(状态机、节点、条件边) | **自己** | 项目灵魂,面试必问;AI 只 review |
| `app/gitops/`(快照/diff/应用/回滚) | **自己** | 属于"必须掌握"清单 |
| `app/executor/local_runner.py`、`whitelist.py` | **自己** | 命令执行边界,安全核心 |
| `app/tools/` 协议定义与边界校验 | **自己**(实现细节可 AI 补) | 工具协议是 Agent 的接口契约 |
| `app/adapters/pytest_adapter.py` 失败签名提取规则 | **自己** | 判定规则不可外包 |
| `app/evals/metrics.py` 指标判定逻辑 | **自己** | 数字要自己能解释 |
| `app/api/`、`app/storage/` | AI(自己审 schema 与并发点) | 标准 CRUD,学习计划允许交给 AI |
| `app/executor/docker_runner.py`、`docker/` | AI(自己做隔离验证实验) | 配置类,但边界要自己实验验证 |
| `app/llm/`(模型 SDK 薄封装) | AI | 学习计划明确允许 |
| `bugs/` 题面与代码 | AI 生成 + **自己审题** | 防止题目退化或互相重复 |
| CI、报告渲染、样板代码 | AI | 同上 |

## 3. 任务卡格式与节奏

每张卡固定五要素:

```text
### T<m>.<n> <标题>
- 产出:  <文件路径>
- 依赖:  <其他卡>
- AI:    [自己写 | 自己写+AI测试 | AI写+自己审 | AI]
- 验收:  <命令 + 期望结果>
- 自查:  <收工前必须能解释的问题>
```

**每次开工仪式**(每张卡都一样):

1. 打开任务卡,把"卡内容 + AGENTS.md + 相关现有文件"一起交给 AI;
2. 让 AI 先复述实现计划(改哪些文件、加什么测试),确认无误再生成;
3. 生成后执行三步验收;
4. 通过后 `git commit`(格式见第 4 节),卡标记完成。

## 4. 工程约定(M0 一次性建立,之后全程遵守)

- Python 3.11+,全量类型标注;`ruff` 负责 lint + format;`pytest` 是唯一测试框架;
- 异常分层:`TaskError / PatchError / ExecError / GateError / BudgetError`,API 层兜底转统一错误结构 `{code, message, task_id}`;
- 日志:stdlib `logging`,每条含 `task_id` 与 `request_id`;
- 配置:`pydantic-settings` + `.env`,密钥不进仓库(`.gitignore` 首日建好);
- commit 格式:`feat(gitops): 支持 worktree 快照`,每张卡至少一个 commit;
- 仓库根放一份 `AGENTS.md`(AI 会话约定):项目结构、编码规范、禁区(不改 `tests/` 期望行为、不动门禁规则)、生成代码要求(类型标注 + 附测试)。

## 5. 里程碑总览

| 里程碑 | 周 | 主题 | 对应企划书阶段 | 前置学习模块 |
|---|---|---|---|---|
| M0 | 1 前半 | 工程脚手架与约定 | 阶段二准备 | L0/L1 |
| M1 | 1—2 | Git 操作基座 | 阶段二 | L0 |
| M2 | 2 | 测试执行器与报告解析 | 阶段二 | L1/L2 |
| M3 | 2—3 | 纯 Python Agent 循环 | 阶段一/二 | L3 |
| M4 | 3—4 | Bug 集 v0 + 单任务闭环 | 阶段二 | L4 |
| M5 | 4—5 | LangGraph 状态机迁移 | 阶段二/三 | L4 |
| M6 | 5—6 | 质量门禁与回滚 | 阶段三 | L2/L4 |
| M7 | 6—7 | 服务化(API + SQLite) | 阶段四 | L5/L7 |
| M8 | 7 | Docker 隔离 + Redis + Compose | 阶段四 | L6/L7 |
| M9 | 8+ | 评测、报告与收尾 | 阶段五 | L7 |

(企划书阶段一"理解 mini-SWE-agent"无代码产出,由学习计划 L3 承担,与 M1—M3 并行。)

---

## 6. 里程碑详解

### M0 工程脚手架(第 1 周前半,约 1 天)

- [ ] **T0.1 初始化仓库**
  产出:`pyproject.toml`、`.gitignore`、`ruff.toml`、`AGENTS.md`、`README.md`
  AI:AI 写 + 自己审
  验收:`ruff check . && pytest` 通过(允许 0 个测试)
  自查:能解释 pyproject 里每个依赖的用途
- [ ] **T0.2 建目录骨架**
  产出:`app/{api,graph,tools,executor,gitops,adapters,storage,llm,evals}`、`tests/`、`bugs/`、`docker/`、`docs/`、`scripts/`,各目录带 `__init__.py` 与一句话 README
  AI:AI
  验收:目录树与企划书第 8 节一致
- [ ] **T0.3 CI 流水线**
  产出:`.github/workflows/ci.yml`(ruff + pytest)
  AI:AI
  验收:push 后 GitHub Actions 绿
- [ ] **T0.4 fixture 测试仓库**
  产出:`tests/fixtures/demo_repo/`(一个带 `parse_date` 函数、3 个测试、其中 1 个故意失败的小仓库,做成 git 仓库 fixture)
  AI:AI 生成 + 自己审
  验收:`pytest tests/fixtures` 收集无错误;手工 `git -C tests/fixtures/demo_repo log` 有 ≥2 个 commit
  自查:为什么 fixture 仓库要预先 commit(答:apply/diff/回滚都依赖 HEAD)

### M1 Git 操作基座(第 1—2 周,2—3 天)—— 本里程碑自己写

- [ ] **T1.1 快照与工作区** `app/gitops/snapshot.py`
  功能:把目标仓库复制/检出为隔离工作区,固定 commit,记录基线 HEAD。
  AI:自己写(AI 只 review)
  验收:对 demo_repo 建工作区后,改动并删除工作区,源仓库 `git status` 干净
  自查:为什么复制工作区而不是直接在源仓库上打补丁
- [ ] **T1.2 diff 封装** `app/gitops/differ.py`
  功能:`git_diff` 工具的实现,返回 unified diff 文本与变更文件列表。
  AI:自己写
  验收:手工改一个文件后能拿到含 `---/+++/@@` 的 diff 与 `changed_files` 列表
- [ ] **T1.3 补丁应用** `app/gitops/patcher.py`
  功能:`apply_patch` 工具的实现——先 `git apply --check`,通过后 apply,返回结构化结果(成功/拒绝原因)。
  AI:自己写 + AI 生成测试用例
  验收:`pytest tests/test_patcher.py` 覆盖:正常应用、check 失败、patch 损坏三类
  自查:`--check` 拦截了什么,为什么先 check 再 apply
- [ ] **T1.4 回滚** `app/gitops/rollback.py`
  功能:`reset_workspace` 工具的实现,恢复到基线 HEAD(含未跟踪文件清理)。
  AI:自己写
  验收:改动 + 新增文件后回滚,`git status` 与基线一致
  自查:为什么用 `reset --hard` + `clean` 而不是 `revert`
- [ ] **T1.5 gitops 集成测试** `tests/test_gitops.py`
  AI:AI 写用例 + 自己补边界(CRLF、二进制文件、路径含 `..`)
  验收:demo 脚本 `python scripts/demo_gitops.py` 完整走一遍 diff → apply → rollback 并打印每步结果

### M2 测试执行器与报告解析(第 2 周,2 天)

- [ ] **T2.1 本地执行器** `app/executor/local_runner.py`
  功能:`run_tests(cmd, cwd, timeout) -> TestResult`;`subprocess` + `capture_output` + 超时杀进程树(Windows/Unix 双路径)+ 输出截断。
  AI:自己写
  验收:对 demo_repo 跑 `pytest`,返回 exit_code、stdout 尾部、耗时;超时用例(死循环测试)能被终止且不残留子进程
  自查:Windows 下为什么 `proc.kill()` 不够
- [ ] **T2.2 命令白名单** `app/executor/whitelist.py`
  功能:`check_cmd_allowed(cmd, whitelist)`;`shell=False` 参数列表校验,禁止重定向/管道/拼接。
  AI:自己写
  验收:`pytest -q "x; rm -rf /"` 类输入被拒;`pytest tests/test_foo.py::test_a` 放行
  自查:能演示一个 shell=True 注入例子
- [ ] **T2.3 pytest 适配器** `app/adapters/pytest_adapter.py`
  功能:用 junitxml/json 报告解析出 `{passed, failed, failed_cases: [{name, signature, message}]}`;`signature` 由"测试名 + 断言类型"归一化生成。
  AI:接口定义与 signature 规则自己定,解析代码可 AI 补
  验收:对 demo_repo 的失败输出能提取出那 1 个失败测试及签名
  自查:失败签名为什么必须归一化(答:跨轮次比较"同一个失败")
- [ ] **T2.4 执行器单测** `tests/test_executor.py`
  AI:AI 写 + 自己审
  验收:超时、白名单拒绝、解析三类用例全绿

### M3 纯 Python Agent 循环(第 2—3 周,3—4 天)—— 核心中的核心

- [ ] **T3.1 工具集** `app/tools/`(7 个工具函数 + `registry.py`)
  功能:企划书第 5 节的 7 个工具;`registry.py` 统一管理名称、参数 schema、边界校验、调用入口。
  AI:每个工具的边界校验逻辑自己写,函数体可 AI 补
  验收:`registry.list_tools()` 返回 7 个工具;每个工具的拒绝路径(越权路径、超行数、非白名单命令)有单测
- [ ] **T3.2 轨迹记录器** `app/tools/tracker.py`
  功能:按企划书 5 节 JSON 格式写 JSONL(先落文件,入库在 M7)。
  AI:格式定义自己定,写入逻辑 AI 可补
  验收:跑一轮后 JSONL 每行可 `json.loads` 且字段齐全
- [ ] **T3.3 主循环** `app/graph/plain_loop.py`
  功能:纯 Python while 循环——组装系统提示 → 调模型 → 解析工具调用 → 执行 → 结果回填 → 终止判断(最大轮数 / 模型声明完成)。
  AI:**自己写**,AI review
  验收:对 BUG-001 跑完一轮,`runs/<task_id>/trajectory.jsonl` 有完整记录
  自查:不看资料默写这个循环的伪代码(学习计划 L3 验收项)
- [ ] **T3.4 模型封装** `app/llm/`
  功能:模型 SDK 薄封装 + 可替换的 FakeLLM(录制/回放,供后续测试)。
  AI:AI
  验收:FakeLLM 回放模式下 plain_loop 全程不联网可跑
- [ ] **T3.5 循环单测** `tests/test_plain_loop.py`
  AI:场景自己设计,FakeLLM 用例 AI 写
  验收:覆盖"一轮成功""连跑 N 轮超预算终止""工具报错回填后继续"三个场景

### M4 Bug 集 v0 + 单任务闭环(第 3—4 周,2—3 天)

- [ ] **T4.1 题目规范** `bugs/README.md` + `manifest.yaml` schema
  内容:企划书 11.1 的目录规范;manifest 字段(failed_tests / regression_tests / allowed_paths / max_rounds / budget)。
  AI:自己写
- [ ] **T4.2 五道开山题** `bugs/BUG-001..005/`
  覆盖:异常处理、边界条件、类型错误、数据访问、跨文件定位各一道。
  AI:AI 生成候选 + 自己逐题审(跑通"基线失败"才算合格)
  验收:每题 `pytest` 基线恰好失败指定测试、回归测试全过
- [ ] **T4.3 单任务驱动器** `app/evals/run_single.py`
  功能:命令行入口——建任务 → 快照 → 基线 → 循环 → 产出 `runs/<task_id>/{diff.patch, report.json, trajectory.jsonl}`。
  AI:自己写
  验收:`python -m app.evals.run_single --bug bugs/BUG-001 --max-rounds 5` 三件套齐全
- [ ] **T4.4 修通 3—5 题**
  AI:调 prompt(系统提示、工具描述),不改核心逻辑
  验收:≥3 题 resolved,成功与失败轨迹都保留
  自查:能对着 trajectory 讲出模型每一轮在干什么

### M5 LangGraph 状态机迁移(第 4—5 周,3—4 天)—— 自己写

- [ ] **T5.1 状态定义** `app/graph/state.py`
  功能:`TaskState` TypedDict——`status / round_count / failure_signature / changed_files / test_results / budget_used / messages`。
  AI:自己写
  自查:每个字段被哪些节点读、哪些节点写
- [ ] **T5.2 节点函数** `app/graph/nodes.py`
  功能:baseline / localize / propose / apply / verify 五个节点,逻辑从 plain_loop 重构复用。
  AI:自己写,AI 帮做重构机械活
  验收:重构后 M4 的 3 题依旧 resolved(回归保障)
- [ ] **T5.3 图与条件边** `app/graph/builder.py`
  功能:按企划书 4.2 转移表建图;预算检查作为全局守卫节点/边。
  AI:自己写
  验收:转移表每条边至少一个测试用例(用 FakeLLM 驱动到对应分支)
  自查:白板画出完整状态图(学习计划 L4 验收项)
- [ ] **T5.4 checkpoint** `app/graph/checkpoint.py`
  功能:SqliteSaver 接入,任务可恢复。
  AI:AI 配置 + 自己弄懂存储内容
  验收:跑到 VERIFY 时杀进程,重启后从 checkpoint 恢复继续
- [ ] **T5.5 plain_loop 退役**
  验收:入口切换到 LangGraph 实现,`plain_loop.py` 保留作为教学参考并注明

### M6 质量门禁与回滚(第 5—6 周,3 天)—— 判定规则自己写

- [ ] **T6.1 门禁引擎** `app/graph/gates.py`
  功能:企划书第 9 节六项门禁(格式/文件/路径/范围/命令/资源),每项独立函数、可配置、返回结构化违规记录。
  AI:规则自己写,表驱动测试 AI 生成
  验收:每项门禁 ≥3 个用例(通过/拒绝/边界)
  自查:六项门禁各拦截什么攻击,不看文档列出来
- [ ] **T6.2 回滚编排**
  功能:VERIFY 失败/回归 → rollback → round+1 → PROPOSE_PATCH;预算超限 → BUDGET_EXCEEDED 保留现场。
  AI:自己写
  验收:构造一个"模型反复失败"场景(FakeLLM),最终停在 BUDGET_EXCEEDED 且工作区与基线一致
- [ ] **T6.3 转人工**
  功能:NEEDS_REVIEW 状态 + 报告中生成人工复核建议(失败签名、已尝试补丁、建议检查点)。
  AI:状态逻辑自己写,建议文案模板 AI 写
- [ ] **T6.4 攻击案例集** `bugs/attacks/`
  案例:①补丁修改 `tests/`;②路径 `../../` 穿越;③新增超范围文件;④补丁删除失败断言。前两个必做。
  AI:自己构造
  验收:每个案例被对应门禁拦截为 PATCH_REJECTED,轨迹与违规详情落盘
- [ ] **T6.5 回滚案例**
  验收:企划书 13 节"至少一个回滚案例"达成,过程可在轨迹中回放

### M7 服务化:API + SQLite(第 6—7 周,3—4 天)—— AI 写,自己审

- [ ] **T7.1 建表与仓储** `app/storage/`
  功能:企划书第 6 节五张表 + Repository 层;trajectory 从 JSONL 改为入库(文件副本保留)。
  AI:AI 写 + 自己审 schema(索引、事务边界)
  验收:五张表 CRUD 单测;`trajectory_events` 插入 1 万条的查询走索引(EXPLAIN 验证)
- [ ] **T7.2 API 端点** `app/api/`
  功能:企划书第 7 节六个端点 + Pydantic schema + 统一错误结构。
  AI:AI
  验收:httpx 测试覆盖每个端点的 200/404/422/409
- [ ] **T7.3 后台执行**
  功能:POST /tasks 入队后台跑,GET 轮询状态;服务重启时 RUNNING 态任务标记 NEEDS_REVIEW(防僵尸)。
  AI:AI 写 + 自己审并发点
  验收:创建任务后立即可查到 CREATED/RUNNING;重启测试通过
- [ ] **T7.4 报告导出** `app/api/report.py`
  功能:report.json + Markdown 渲染(状态、轮数、diff、前后测试对比、预算消耗)。
  AI:AI
  验收:`GET /tasks/{id}/report` 可下载且字段与库内一致
- [ ] **T7.5 端到端测试** `tests/test_e2e.py`
  验收:API 创建 → 轮询 → 报告全流程测试绿;数据库文件删除重建后流程仍通

### M8 Docker 隔离 + Redis + Compose(第 7 周,3—4 天)

- [ ] **T8.1 执行器镜像** `docker/executor.Dockerfile`
  AI:AI;验收:镜像内 `pytest` 可用,体积 < 500MB
- [ ] **T8.2 容器执行器** `app/executor/docker_runner.py`
  功能:`--rm --network=none --memory=1g --cpus=1` + workspace 只读挂载源、可写挂载副本。
  AI:AI 写 + **自己做隔离实验**
  验收:容器内完成一次完整 VERIFY;隔离实验记录写入 `docs/docker-isolation-notes.md`(尝试访问宿主文件/联网的结果)
  自查:Docker 隔离了什么、没隔离什么(学习计划 L6 验收项)
- [ ] **T8.3 任务锁与幂等** `app/storage/locks.py`
  功能:Redis `SET NX EX` 实现同 repo+commit+issue 幂等;锁 TTL 与崩溃释放。
  AI:AI 写 + 自己能解释三个时机(加锁/释放/超时)
  验收:并发两次创建同一任务返回同一 task_id;杀进程后锁 ≤TTL 自动释放
- [ ] **T8.4 编排** `docker/docker-compose.yml`(api / redis / executor 基础镜像;SQLite 先用 volume)
  AI:AI
  验收:`docker compose up` 后 T7.5 的端到端测试全绿

### M9 评测、报告与收尾(第 8 周起,持续)

- [ ] **T9.1 Bug 集扩充到 20—40 题**
  AI:AI 生成候选 + 自己逐题审(查重、验基线、验可修性)
  验收:四类缺陷各 ≥1/4;简单/中等两档;每题基线可复现
- [ ] **T9.2 指标计算** `app/evals/metrics.py`
  功能:企划书 11.2 七个指标,判定逻辑全部脚本化。
  AI:判定规则自己写,报表聚合 AI 补
  验收:对 runs/ 目录计算出一套完整指标;同输入两次运行结果一致
- [ ] **T9.3 评测报告** `app/evals/report.py`
  功能:Markdown 总表 + 分类表现 + 失败案例复盘(引用轨迹)。
  AI:AI
  验收:`python -m app.evals.report --out docs/eval-report.md` 一键生成;成功与失败任务都在报告里
- [ ] **T9.4 CI 集成**
  验收:GitHub Actions 产出报告 artifact;badge 挂 README
- [ ] **T9.5 失败复盘 ≥5 篇** `docs/postmortems/`
  格式:学习计划第 4 节模板
- [ ] **T9.6 最终文档**
  产出:README(架构图 + 快速开始)、`docs/design.md`(设计决策)、简历叙事按企划书第 14 节用真实数字回填
  验收:对照企划书第 13 节完成标准逐条打勾,全勾才进简历

---

## 7. Prompt 模板库

**P1 脚手架/样板生成**(T0.x、T7.x 常用)

```text
请在 <目录> 下按以下规格实现 <模块>:
- 功能: <一句话>
- 接口: <函数/类签名>
- 约束: 全量类型标注;ruff 通过;异常用 <XxxError>;日志带 task_id
- 测试: 附 pytest 用例,覆盖 <正常/边界/拒绝> 三类
- 禁止: 修改规格外的任何文件
先复述你的实现计划,确认后再生成。
```

**P2 测试用例生成**(每张"自己写"的卡的配套)

```text
为 <文件路径> 生成 pytest 用例,必须覆盖:
<列出边界场景,如: 正常应用 / check 失败 / 修改 tests/ / 路径含 .. / 二进制 patch>
使用 tmp_path fixture,不依赖网络,不 mock 被测对象本身。
```

**P3 代码审查**(AI 写完自己审时)

```text
以下是 diff。只列问题,不要重写:
1) 与规格不符之处;
2) 边界条件与错误处理遗漏;
3) 安全问题(命令注入 / 路径穿越 / 资源泄漏 / 并发);
4) 测试缺口。
<贴 diff 与原规格>
```

**P4 调试**(闭环跑不通时)

```text
任务在 <状态> 卡住。附: trajectory.jsonl 最后 N 行 + 失败输出。
请先给出 3 个以内的根因假设并排序,标注每个假设需要什么证据验证;
未经我确认不要直接改代码。
```

**P5 题目生成**(T4.2 / T9.1)

```text
生成一道 PatchPilot Bug 任务,要求:
- 小型 Python 仓库(<10 文件),缺陷类型: <边界条件/异常处理/...>;
- 基线恰好 1—2 个测试失败,其余测试通过;
- 修复不需要改测试文件,改动 ≤2 个文件;
- 输出: repo/ 文件树与内容 + issue.md(面向 Agent 的描述)+ manifest.yaml。
生成后自行验证基线失败与可修复性,给出验证输出。
```

## 8. 风险与降级策略

**卡住处理:** 单张卡卡住超过 2 小时 → 停手写失败复盘(状态/签名/根因)→ 换三种问法问 AI → 仍不通则按下面顺序降级,并在 `docs/postmortems/` 记录。

**降级顺序**(从增强到核心,砍前面的不砍后面的):

1. 模型对比、上下文压缩、代码结构检索(阶段五增强项);
2. PostgreSQL 迁移(SQLite 到底);
3. checkpoint 恢复(降级为"失败任务重跑");
4. 评测集 40 题降到 20 题;
5. Docker Compose 单机直跑(Redis 保留,内存锁兜底再降)。

**不可砍红线:** 六项门禁、回滚、trajectory 落盘、判定规则、评测报告——这些是企划书完成标准的硬项,砍了项目就不进简历。

**进度预警:** 连续两个里程碑周末自检不过 → 停新功能,回头补学习计划对应模块。

## 9. 里程碑完成即检查

每个里程碑收尾时过一遍(全部满足才开下一个):

- [ ] 本里程碑所有任务卡验收命令跑绿;
- [ ] `ruff check . && pytest` 全绿,CI 绿;
- [ ] "自己写"区的代码都能脱稿复述核心逻辑;
- [ ] 本里程碑产出物(轨迹/案例/笔记)已 commit 落盘;
- [ ] 对照学习计划周表完成当周自检。

## 10. 最终交付检查单(对应企划书第 13 节)

- [ ] API 与命令行均可创建完整任务;
- [ ] ≥3 题修复成功且成功/失败轨迹齐全;
- [ ] ≥1 个补丁被门禁拦截(`bugs/attacks/`);
- [ ] ≥1 个回滚案例 + ≥1 个转人工案例;
- [ ] 20+ 题 Bug 集 + 七项指标 + 一键可复现评测报告;
- [ ] 自动化测试与 CI 绿;
- [ ] ≥5 篇失败复盘;
- [ ] 能脱稿讲清:参考项目设计、自己的改动、未实现的部分。
