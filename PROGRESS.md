# PROGRESS.md — 升级循环改动日志

每完成一个原子任务追加一条:改了什么 → 为什么 → 机器证据(commit / 测试数 / 回放对照)。
时间戳用绝对日期。D 段是开工前的诊断基线,后续争议都回到这里对账。

## D. 开工诊断基线(2026-10-07,HEAD `02f0f90`)

- 基线状态:`git status` 干净,`ruff check .` 与 `pytest -q` 见 D.0。
- D.1 消息列表只有一个构建点:`app/graph/plain_loop.py:148-151`(seed)→ `:201`/`:217`/`:272`(追加)。
  `model.complete` 全仓只有一个调用点(`plain_loop.py:184`)。
- D.2 历史**不做任何裁剪**:在 `app/` 里搜 `trim|sliding|window|drop_oldest|prune|compress|digest|summariz`
  零命中(仅 `patching.py:36 _gate_skeleton` 无关命中)。超预算的唯一出口是 `BudgetError`
  (`plain_loop.py:173-182`),而不是把上下文压小。
- D.3 跨阶段/跨轮只有**文本通道**:`messages` 是 `run_plain_loop` 的局部变量,每次调用冷启动
  (`nodes.py:381` 注释自证"全新会话必须带全 Bug 描述与定位结论")。`findings` 来自模型自己的
  `finish(summary=...)`(`plain_loop.py:221-222` → `nodes.py:300`),降级版 `_provisional_findings`
  是本地拼接(`nodes.py:317-358`,注释"纯本地拼接,不多花一次请求")。
- D.4 无持久记忆:仓库文件树/模块大纲/符号索引**从未预注入**;结构只能靠模型主动 `list_files`
  (`app/tools/files.py:36-45`,`MAX_LIST_FILES=500`)。
- D.5 逐结果折叠只有一处:`plain_loop.py:267-271` → `fold_output(head=40, tail=15, hard_cap=8000)`
  (`app/tools/output_filter.py:100-113`);`hard_cap` 是函数默认值,不是 Settings 键。
  `reasoning_content` 每轮原样回传(`plain_loop.py:113-116`,`4a4093e` 修的思维链 400),CoT 会逐轮累积。
- D.6 检索是 Python 子串:`app/tools/files.py:79,93-94` 用 `needle in line.lower()` + `workspace.rglob("*")`,
  无上下文行/无单文件上限/无排序,只有全局 `max_search_results=50` 与每条 200 字符截断。
  **无任何 AST/符号能力**(全仓 `ast` 只有一处:`patcher.py:138` 用 `ast.parse` 做应用后语法预检)。
- D.7 编辑范式已达前沿:块协议锚点局部替换(`app/gitops/blockpatch.py:283-365`,
  0 命中 → `anchor_not_found`、多命中 → `ambiguous_anchor`、乱序 → `chunks_out_of_order`),
  明确拒绝 unified diff(`registry.py:82-83`)。此项**不重构**,只补错误可读性。
- D.8 无独立 Plan 阶段:搜 `plan|planning|structured_plan` 在 graph/prompt 层零命中;
  "计划"只是 `LOCALIZE_PROMPT` 要求写进 `finish.summary` 的自由文本(`prompts.py:57`)。
- D.9 checkpointer 纯留档:`SqliteSaver` 逐 superstep 写 `run_dir/checkpoints.sqlite`
  (`runner.py:99-123`),但全仓搜 `get_state|StateSnapshot|update_state|resume|replay` **零命中**;
  `app/graph/checkpoint.py:2-6` 自己写明"无崩溃重放路径"。ToolContext 在闭包里不进 state
  (`state.py:4-5`),跨进程续跑结构性不可能。
- D.10 中断处理是判死不是续跑:`service.recover_stale()` → `repo.recover_stale_running()`
  把 RUNNING/QUEUED 僵尸一律标 NEEDS_REVIEW 并释放锁(`repository.py:170-188`)。
  `app/api/recycle.py` 不是 reaper,只回收 FINISHED 任务的 workspace/checkpoints。
- D.11 实测死因(付费证据,`runs/swe-hard-graph*`):真实多文件题两道 sphinx **死在 LOCALIZE 只读调查**
  (16-19 轮、417,894 tok 撞 400k 份额顶,`apply_patch` 0 次);降级续跑后 PROPOSE 仍
  13 次 search / 10 次 read 且 `apply_patch` 两次均 0 次 ⇒ 上下文只增不减 + 无骨架先验 = 烧穿预算。
  这条是 M1/M2 排最前的唯一依据,不是凭感觉。

## 0. 开工基线

- 开工时 `git status` 干净,HEAD `02f0f90`。
- 全量基线:`536 passed, 2 skipped, 3 warnings in 1120.80s (0:18:40)`(exit 0)。
  两条 skip 是 `tests/test_locks.py` 无可用 Redis;2 skipped 是常态口径。
- 以下条目**按里程碑归组**,不按落笔先后排:M2 的条目写在 M1 之前,因为这两张卡被合并进了
  同一个 commit(原因见文末「流程教训」),阅读顺序以 TODO 的 M1→M7 为准。

## M2 仓库骨架持久记忆(2026-10-07)

- 新增 `app/context/repo_map.py`(179 行):`build_repo_map(workspace, max_chars, max_files)`
  纯本地、确定性、零请求。两档共用一份字符预算——文件树(相对 POSIX 路径,排序天然按顶层目录
  聚组)+ `*.py` 的 stdlib `ast` 符号大纲(`class Foo (L10-L40)` / `def bar(a, b=1) -> int (L12-L20)`,
  含 `async def`、装饰器前缀、posonly `/`、裸 `*`、kwonly、`*args/**kwargs`、返回标注);
  类只嵌一层,**函数体整体不进**(局部函数不是仓库结构)。
  `SKIP_DIRS` 直接 import 自 `app.tools.files`——骨架与 `list_files` 对"仓库里有什么"永不两样。
- 三条降级规则(都是刻意的):① 单文件 `ast.parse` 失败只让该文件"进树不进大纲",坏文件不能
  带走整份骨架;② `repo_map_for_workspace` 外壳吞掉任何异常并 `log.warning` 返回空串——骨架是
  **优化项**,可选上下文块没有资格把任务打成失败;③ 预算放不下"头部 + 截断行"时**整块不发**,
  而不是发一份误导性的半骨架。截断对模型可见:末行必出 `… skeleton truncated: X files omitted`,
  且按整块进出、绝不发半截行。符号链接与非 UTF8 只进树不解析。
- 接线 `app/graph/nodes.py`:新增 `_persistent_context()`,只在 **localize 与 propose** 两个调用点
  把骨架追加到 `extra_system`(分支候选点刻意未接,保持单变量对照)。关闭时
  `_persistent_context` 返回 `""`,而 `plain_loop` 的拼接是 `SYSTEM_PROMPT + (…if extra_system…)`,
  于是系统提示**逐字不变**——该形状被用例直接钉住(记录首轮请求体比对 `SYSTEM_PROMPT`),
  另有一条源码级护栏用例证明只有这两个调用点带骨架。
- `app/config.py`:`repo_map_enabled=True`(默认开启——这是"上线"而不是"埋开关",
  持久记忆若默认关闭等于没做)、`repo_map_max_chars=4000`、`repo_map_max_files=200`,
  带校验器。三键均进 `provenance.SNAPSHOT_KEYS`(决定模型第一轮可见哪些文件与符号,
  同题开/关骨架的轨迹不可比),`tests/test_driver.py` 的字面键集合同步扩列。
- 用例:`tests/test_repo_map.py` **12 例**(`--collect-only` 实测条数;子代理自报"13 例"多计一条,
  已按实测更正)(行号正确性/嵌套一层/局部函数不出/签名重建各形态/
  坏文件降级且好文件仍在/非 py 只进树/二进制只进树/SKIP_DIRS/截断无半截行/`max_chars=0`/
  空仓库/外壳异常降级/开关两态的提示词形状与分支候选点仍只拿变体提示的源码护栏)。
- 证据:`ruff check .` 全过、`ruff format --check .` 161 files;定向 `80 passed`
  (`test_repo_map + test_plain_loop + test_driver + test_localize_budget_share + test_branching
  + test_docs_anchors`),默认开启额外拉动了图路径,故另核 `tests/test_graph.py` → `25 passed`;
  **M1+M2 合并态全量 `569 passed, 2 skipped`(18:42,exit 0)**,条数可完全对账:
  开工基线 536 + M1 的 21(`test_token_window` 15 + `test_plain_loop` 新增 6)
  + M2 的 12 = 569。
- 行锚漂移:`tests/test_docs_anchors.py` 的 `nodes.py` 锚点 465→470(M1)→496(M2),文案未动。



## M7 的前置测量工具(2026-10-07)

- 新增 `scripts/compare_batches.py`:两个零成本回放批次的**逐题**对照。
  判定字段(`status/verdict/outcome/rounds/gate_violations/changed_files/baseline_failed/
  baseline_regression_ok/verify_failed_ok/verify_regression_ok/error`)任一不等 → 退出码 1;
  成本字段(`turns/tokens_used/duration_ms`)只报差异不改退出码——避免把"轨迹形状变了"
  误报成行为回归,也避免把行为回归混在成本差异里放过。
  `--tools` 附 `trajectory.jsonl` 的逐阶段工具直方图与 `context_compact` 事件计数,
  这是判断 M2/M3 是否真的让模型"少查几轮"的唯一口径(单看 tokens 会被 fake 估算器噪声淹没,
  见 [[env-local-verify-gotchas]] 第 0g 条)。同 bug_id 出现两次直接拒绝,不静默去重(复盘 R-1 的坑)。
- 自证:`compare_batches.py runs/graph35-v2 runs/graph35-v2 --tools` → 35 题、
  `合计 turns 249 -> 249 | tokens 6917 -> 6917`、"判定字段逐题一致",python 退出码 0
  (退出码按 [[env-local-verify-gotchas]] 0c 的规矩单独取,不接管道)。
  `ruff check` / `ruff format --check` 单文件通过。
- 写它时踩到两处自己的错:① 一处 `int(rows.get(f))` 里的 `f` 是上一个推导式的作用域残留 →
  `NameError`,被自证跑出来了;② 把脚本输出重定向到 `/tmp/...` 后 Windows 侧 python 读不到
  (同一坑的第 0e 条),改成仓库内临时文件再删。

## M1 工作记忆滑动窗口压缩(2026-10-07)


- 新增 `app/context/token_window.py`:`compact_messages()` 纯函数,零 LLM 调用、零文件系统访问。
  不可压区 = 第 0 条 system + 首条 user + 最近 `keep_recent_turns` 个回合组;
  Pass A 把可压区的 tool 结果换成**带工具名与关键入参**的单行存根(反查 `tool_calls` 的
  `tool_call_id`),Pass B 才整组丢弃,且 assistant 与其 tool 回执同进同出(不留孤儿 tool)。
  `reasoning_content` 只压成 `[compacted reasoning]` 而**不删除**——思考模式端点缺字段直接 400
  (见 D.5 与 `4a4093e`)。token 估算统一复用 `app.llm.base.messages_tokens`,与循环同口径。
- 接线 `app/graph/plain_loop.py`:新增两个关键字参数(默认关闭,老调用点行为逐字不变),
  在 turn 边界**先压缩再走原预算门禁**;压缩真发生时记一条 `context_compact` 轨迹事件。
  `BudgetError` 条件、消息形状、`_budget_error` 载荷、cancel/时间预算检查的顺序**一字未改**。
- `app/graph/nodes.py` 只在 3 个 `run_plain_loop` 调用点(localize / propose / branch 候选)
  透传 Settings 两键,未触碰任何门禁与预算逻辑。
- `app/config.py`:`context_window_tokens`(软阈值,**默认 0 = 关闭**)、`context_keep_recent_turns=6`
  (带 `>=1` 校验器)。默认关闭是为了让 M7.1 的零成本对照回放先出数,再定生产默认值——
  长期停在 0 等于机制没上线,该项已作为 M1.5 记在 TODO。
- 分类修正:这两个键进 `app/evals/provenance.py::SNAPSHOT_KEYS` 而非 EXEMPT。
  理由:软阈值决定"模型第 k 轮看到多少历史",同题开/关压缩的轨迹不可比,与
  `localize_budget_share` 同类;`refine_*` 只截单条工具回执,不同类。
- 用例:`tests/test_token_window.py` 15 例(关闭/未超/头部钉住/尾部字节一致/Pass A 早停/
  存根含工具名/短结果不压/思维链压而不删/Pass B 不留孤儿 tool/目标不可达返回最小结果/
  无可压区/入参不被就地修改/确定性/共用估算器口径);`tests/test_plain_loop.py` 6 例接线
  (FakeLLM 长轨迹仍在阈值内 finish、事件留痕、pinned 不被压、`context_window_tokens=0`
  时消息序列与压缩前逐字一致)。
- 证据:`ruff check .` 全过、`ruff format --check .` 159 files already formatted;
  定向 83 passed;M1 单里程碑态全量 **`557 passed, 2 skipped`(18:51,exit 0)**
  (开工基线 536 → M1 新增 21 例;2 skipped 是 `test_locks.py` 无 Redis 的常态口径)。

## 流程教训(本夜)

- M1 与 M2 由两个子代理接力实现,结果**共用** `app/config.py`、`app/graph/nodes.py`、
  `app/evals/provenance.py`、`tests/test_driver.py` 等文件且中途无提交点——两里程碑已无法拆成
  两个独立可编译的 commit,只能合并提交并在正文分节说明。**规则(后续每个里程碑遵守):
  子代理交付并被我复核后立刻 commit,再开下一个里程碑的实现**;否则"每卡一 commit"会在
  共享文件上失效。
- 并发验证的正确姿势:两条 pytest 各带**互不相同**的 `--basetemp` 时,全量与定向子集可以同时跑
  (本轮实测两边都干净)。派子代理写模块时,任务书里就写死"只跑定向用例 + 专属 basetemp、
  禁止跑全量",全量由主代理串行做——否则子代理会自己撞出 `364 errors` 这种环境冲突假失败,
  并把时间耗在自证上。
- 全量在 collection 时就定型:子代理中途改文件一般不影响已在跑的进程,所以"某行 `N passed`
  归给哪个状态"要按**测试条数算术**核对(557 = M1 完整态减掉我复核时删去的一例),
  不能只看数字涨了就算证据。
- 复核子代理的两处判断并回改:① 它把 M1 两个键按"与 `refine_*` 同族"归入 provenance EXEMPT,
  实际它们会**整段丢弃历史**、改变轨迹形状,已改判 `SNAPSHOT_KEYS`(与 `localize_budget_share`
  同类);② 它实现了一个无人调用的 `hard_cap_tokens` 参数,已删除——本项目对死键的既有口径是
  删(`round_timeout_seconds`、`run_tests_by_backend` 都是先例)。

