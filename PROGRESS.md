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
- D.12 **检索/列举的可见范围被 500 条上限截断(2026-10-07 复核 M3 时实测发现,已量化)**:
  `app/tools/files.py:_iter_repo_files` 走 `rglob("*")` 排序后 `len(out) >= MAX_LIST_FILES(500)` 就
  break,而 `search_code` 与 `find_symbol` 都以这份清单为遍历域——也就是说**仓库按路径排序后
  第 500 个之后的文件,模型搜不到、列不出、符号也查不到**。这个上限本是为 `list_files` 的
  *输出体量*设的,却顺带裁掉了 *搜索的遍历域*(搜索结果另有 `max_search_results=50` 控制)。
  用平台自己的口径在 9 个已缓存的真实题面仓库上量了一遍(金补丁改动文件在清单里的下标):

  | instance | 仓库文件数 | 金补丁文件位置 | 可见性 |
  |---|---|---|---|
  | sphinx-doc__sphinx-7590 | 1472 | c.py=204, cpp.py=207, **util/cfamily.py=631** | 部分不可见 |
  | sphinx-doc__sphinx-9461 | 1472 | python.py=211, autodoc/__init__.py=230, **util/inspect.py=640** | 部分不可见 |
  | scikit-learn__scikit-learn-12682 | 1416 | **plot_sparse_coding.py=502, dict_learning.py=903** | 全部不可见 |
  | astropy__astropy-13398 | 1924 | 三处在 90/111/112,另一处不在 base 树内 | 部分不可见 |
  | sphinx-doc__sphinx-7748 / 8593 / 8548 | 1472 | 230/233/211 等 | 可见 |
  | astropy__astropy-8707 | 1924 | 409, 427 | 可见 |
  | pydata__xarray-3095 | 233 | 155, 170 | 可见(小仓库) |

  **9 题里 4 题的必改文件根本落在搜索可见范围之外**,而且 sphinx-7590 正是两道死在 LOCALIZE
  (16-19 轮、0 次 apply_patch)的题之一:模型反复 search 拿不到证据,与"目标文件不可见"直接吻合。
  同一条上限也套在 M2 骨架上(`repo_map_max_files=200`,再叠 `repo_map_max_chars=4000`
  ≈ 130 行),所以大仓库的骨架实际只渲染出"字母序最前的一小片",对 sphinx/astropy 这类
  仓库等于给了半张地图。⇒ 拆出 M3.6(遍历域与输出体量两个概念必须分开)与 M2.5
  (骨架在大仓库要出目录级结构而不是字母序文件清单)。

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
  **(M10 更正上面那条"并发姿势":外层 `--basetemp` 互不相同并不够——用例内部用
  `subprocess` 起的嵌套 pytest 不继承 basetemp,一律落在机器默认的
  `Temp\pytest-of-<user>` 根上并发增删编号目录,`os.scandir` 会撞
  `PermissionError [WinError 5]`。本夜 `test_bugset[BUG-015]` 就是这么红的。
  结论改成:套件在跑时**什么都不要再跑**。**
- 复核子代理的两处判断并回改:① 它把 M1 两个键按"与 `refine_*` 同族"归入 provenance EXEMPT,
  实际它们会**整段丢弃历史**、改变轨迹形状,已改判 `SNAPSHOT_KEYS`(与 `localize_budget_share`
  同类);② 它实现了一个无人调用的 `hard_cap_tokens` 参数,已删除——本项目对死键的既有口径是
  删(`round_timeout_seconds`、`run_tests_by_backend` 都是先例)。

## M4 失败反思带上"上一轮改了什么"(2026-10-07,主代理自写)

- 缺陷:verify 失败后 rollback 把工作区硬复位到基线,而下一轮 PROPOSE 是**全新会话**,
  只拿到失败用例的签名与堆栈——"我上一轮到底改了什么"随工作区一起消失了。现有的
  `repeat_streak`(同一组失败连续多轮完全一致)就是原地打转的实测信号,而处置只是加一句
  "请换思路"的文字提示,没有任何事实支撑。
- 新增 `app/graph/reflection.py`:`diff_digest(diff_text, max_files, max_chars)` 从回滚前
  **已保真的 diff** 里只取形状(文件名 / 各文件 +/- 行数 / 第一个 hunk 的上下文),
  `with_discarded_patch(feedback, diff)` 负责追加。
  **刻意不给补丁正文**:正文会诱导模型逐字重放上一版,而形状信息才是"这条路过不通"的证据。
- 接线只在 `nodes.py` 的 rollback 返回体上加一行 `feedback`(用局部 import,
  与该节点既有的 `from app.gitops... ` 同风格,因此不触碰 506 行锚、门禁/预算逻辑一字未动);
  摘要为空时**逐字返回原反馈**——没有回滚过就不许多出一段噪声,这条是回归钉子。
- 用例 `tests/test_reflection.py` 10 例:增删计数不含 `+++`/`---` 头、按 diff 出现顺序、
  `max_files` 裁切并如实标"另有 N 个未列出"、`max_chars` 只按整行裁(断言每行都出现在完整版里)、
  空/非 diff 文本/`max_chars=0` 出空串、追加位置与空反馈两种入口、
  **节点级行为验证**(真 `materialize_repo` 的工作区:回滚后反馈带形状行且工作区确已复位)、
  末轮耗尽仍是 `BUDGET_EXCEEDED` 且 `preserved_diff` 照旧保全(反思不得改变终止判定)。
- 测试自身踩到本机既有坑一次:第一版把形状行写死成 `+2 -0`,实际是 `+19 -17`——
  `core.autocrlf=true` 下追加一行 LF 会让整文件按 CRLF→LF 重写([[env-local-verify-gotchas]] 0d)。
  断言改成 `+N -M` 正则而不是写死数字;顺带一个事实:摘要**如实暴露了换行符改写这件事**,
  这对模型是有用信息(它的"一行修改"实际动了整文件)。
- 证据:`ruff check .` 全过;定向 `59 passed`
  (`test_reflection + test_graph + test_branching + test_docs_anchors`,行锚未漂移)。

## M3.6 + M2.5 检索遍历域解耦与大仓库骨架目录汇总(2026-10-07,主代理自写)


- `app/tools/files.py`:拆成两件事——
  `collect_repo_files(workspace, glob, limit) -> (files, capped)` 是唯一遍历实现,
  `list_files` 继续用 500 条**输出**上限,新增 `search_scope()` 用独立的
  `Settings.max_search_files=8000` 作**遍历域**上限;`search_code`/`find_symbol` 改走后者。
- 诚实性护栏:`search_code`/`find_symbol` 输出新增 `scope_truncated`,
  `list_files` 输出新增 `truncated`(此前只给 count、不告知裁过)。
  理由:没搜全却给一个像"仓库里没有"的空结论,是谎报——这一条直接决定模型会不会反复重查。
- `app/context/repo_map.py`:拆出 `_all_files`,`_dir_rollup(rels, depth)` 按目录聚合
  (`pkg/  (8 files, 8 py)`),文件数超过取样上限时结构档改出目录汇总 + 一行如实说明;
  **小仓库形状逐字不变**(`only.py [lines 2]` 那条快路径没动)。删掉已无人调用的 `_scoped_files`。
- `app/config.py`:`max_search_files=8000`、`repo_map_dir_depth=3`(带校验器),
  两键进 `SNAPSHOT_KEYS`;`nodes.py::_persistent_context` 多传 `dir_depth` 一行。
- 用例 `tests/test_search_scope.py` 10 例:第 600 个文件里的命中搜得到、列表档仍裁 500 但如实标注、
  小仓库不误报裁断、`max_search_files=100` 时拿不到尾部命中但 `scope_truncated=True`、
  深层目录里的符号能跳转、600 文件树上 rg 与 Python 两引擎逐条同输出、两个新键的默认与校验、
  大仓库出目录汇总(被裁的 `zzz/sub/` 也在场)、小仓库形状不变、深度切点把子目录并进祖先。
- 一条**有意的形状变更**并按新口径重写断言(不是为过关而改期望):
  `tests/test_repo_map.py::test_caps_and_degenerate_budgets` 原来钉的是
  "只列前 N 个文件 + `… skeleton truncated: 5 files omitted`",新口径是目录汇总 +
  `… 8 files in repo; directory rollup covers all of them, per-file outlines shown for a
  3-file sample …`。保留的语义是"上限被裁且裁掉的部分对模型可见",改变的语义是
  "可见的方式从'计数'变成'汇总'——因为计数会让整个目录凭空消失"(理由写在该用例里)。
- 我自己的两处错,当场被用例抓出来:新增用例里 `tmp_path / "small"` 忘了 `mkdir()` 就写文件
  (`FileNotFoundError`),以及 `dir_depth=1` 的断言把两个顶层目录的文件数误加成 11;
  `ruff format` 先跑过我一次编辑,导致按旧文本改锚点的 Edit 落空(行锚 505→506 已重钉)。
- 证据:`ruff check .` 全过;定向 4 文件 `85 passed, 1 skipped`;全量在本条 commit 后单独跑。


## M5 严格的 LOCALIZE→PLAN→ACT→VERIFY(2026-10-07,子代理实现 + 主代理复核改动)

- 缺陷(D.8):根本没有计划这一步——"计划如何修复"只是 `LOCALIZE_PROMPT` 让模型塞进
  `finish.summary` 的自由文本(`app/prompts.py:57`),不是工件。付费实跑两次独立同形:
  PROPOSE 13 次 search / 10 次 read、`apply_patch` **0 次**(`runs/swe-hard-graph/3`)——
  拿着单薄的定位摘要就重新调查,而不是动手改。
- 新增 `plan` 节点(`app/graph/nodes.py`)与 `TaskState.plan: str`(可序列化,进 checkpoint):
  一次独立的 LLM 调用产出"目标文件/符号 + 失效机制 + 最小改动意图 + 预期如何被验证证实",
  工具白名单只留 `finish`(刻意:**"先计划"不能变成"再调查一轮"**);
  份额由新键 `plan_budget_share=0.15` 决定,走既有 `_token_budget_for` 与 `ensure_budget` 通道。
- 路由:`localize→plan→propose`;`apply` 的重试边与 `rollback` 的重试边都回到 **plan**
  而不是 propose——失败轮**修订同一份工件**,不是让下一轮冷启动再猜一遍。
  状态字符串一字未改(只改节点指向),所以终态/门禁语义零漂移。
  `runner.py` 的 `recursion_limit` 从 `4N+8` 抬到 `5N+8`(每轮多一个 superstep;
  撞到 LangGraph 递归上限会以框架异常终止,而不是我们的结构化终态——这是必须抬的理由)。
- 计划段降级 ≠ 失败:份额/轮次耗尽时带"模型最后一轮实质文本"进 PROPOSE 并落
  `plan_degraded` 事件(与 localize 的同构处置);**任务级总额耗尽仍是硬终点,N-5 一字未动**。
- `plan_stage_enabled=False` → 零 LLM 请求、零轨迹事件、`state.plan` 恒空,
  于是 PROPOSE 的渲染与引入该阶段之前**逐字节相同**(用例钉住)。
- 脚本模型兼容(这是本卡最大的工程风险):`FakeLLM` 按固定脚本逐步回放,
  多一次调用就会让 `tests/test_graph.py` 的 25 个用例全部错位。
  做法是 PLAN 提示带 `[[plan_stage]]` 标记,`FakeLLM` 认出标记后**不移动游标**直接给
  "计划文本 + finish" 的回合(带 finish 是为了让计划段的循环在第一个 turn 收敛——
  纯文本回合会继续下一轮,那就真的弹脚本步了)。用例断言"计划开/关两次的
  `consumed` 相等且等于脚本长度",把这条兼容性钉死;`tests/test_graph.py` 一行未动。
- **主代理复核后改掉子代理的一处取舍**:它没给 PLAN 注入仓库骨架,理由是
  `tests/test_repo_map.py` 有源码级断言把骨架接线点钉成"只有 localize/propose 两处"。
  我判断这是错的取舍:计划必须点名文件与符号,而实测定位结论常薄到 63-397 字符
  (`runs/swe-hard-graph2` 首次降级 findings 只有 63 字符),只靠它只能写出"改那个模块"
  这类无法执行的话;骨架是本地零请求的,给计划段用正是它最该出现的地方。
  于是 plan 也接 `_persistent_context`,并把那条源码级断言改为"三处接、候选点仍不接"
  (候选点不接是为了保住分支对照的单变量形状)。
- 用例:`tests/test_plan_stage.py`(5 例)+ `tests/test_plan_invariants.py`(6 例)——
  阶段顺序与"零脚本步消耗"的兼容性证明、PROPOSE 的计划块存在性与 plan 空时逐字节相同、
  失败轮回 PLAN 且第二次计划请求带上上一轮反馈、降级仍进 PROPOSE 且落 `plan_degraded`、
  任务级耗尽仍 `BUDGET_EXCEEDED`、`plan_budget_share` 校验、`recursion_limit` 抬升后的多轮跑通、
  `TaskState.plan` 可 checkpoint。
- 消融臂纪律(刻意的不做):`app/evals/single_shot.py` 不加计划段,并在文件里写明
  "计划段是 graph 引擎独有特性,将来任何两臂对照必须把 plan_stage 重新登记进消融变量",
  否则对照就会悄悄多出一个未登记的差异变量(这条纪律来自本轮已被烧掉过一次付费实验)。
- 已知不完整、留给 M7:ADR-0001 与 `docs/design.md` 仍写 `4N+8`;`nodes.py` 已从
  982 行涨到 1150 行(七项门禁锚点 465→470→496→506→621→623),该文件已到需要拆分的规模,
  今晚不动它(禁区文件的大规模重构不是无人值守该做的事),记为待讨论项。
- 证据:M5 定向与图路径用例见 commit 正文;全量 `pytest -q` 在本 commit 前后各核一次。


## M3 ACI 检索升级(2026-10-07)

- 新增 `app/context/ast_outline.py`(130 行):把 M2 骨架用的 AST 助手抽成单一口径
  (`parse_source` / `symbol_entries` / `SYMBOL_KINDS`),`repo_map` 与 `describe_file`/`find_symbol`
  共用——**同一个文件在骨架里看到的符号集合,必须与 describe_file 给出的完全一致**,否则两份
  互相矛盾的地图比没有地图更坏。`repo_map` 的渲染形状逐字未变(它的 12 例用例一行未动)。
- 新增 `app/tools/search.py`(326 行):`search_code` 的取数引擎。**rg 在这里不是第二种语义,
  只是文件级预筛**——这是子代理做差分探针探出来的结论,我复核后认同并保留:rg 剥 BOM、只按
  `\n` 断行,而 Python 的 `splitlines()` 还按 `\r`/`\x0b`/`\x0c`/`\x1c`-`\x1e`/`\x85`/`\u2028`/
  `\u2029` 断行(`\x0c` 分页符在真实 Python 源码里很常见),行号错一位补丁就贴错位置。
  所以行级文本/行号/上下文/裁剪**只有 Python 一条实现**,rg 用 `--files-with-matches` 只回答
  "哪些文件可能含有这个关键词";显式传文件参数(而非目录遍历)既保证遍历域与 `_iter_repo_files`
  完全一致,又避免 rg 走穿 `runs/`、`.pytest-tmp*` 等产物目录(实测一次目录遍历就撞 20s 超时)。
  四条回落:rg 缺席 / 任一批失败或超时 / `regex=True`(Rust 方言 ≠ Python `re`,不给第二套答案)
  / 范围内含符号链接。还有一条方向性护栏:rg 预筛报的文件被 Python 扫出一行都没命中 →
  **整仓复扫**,宁可慢也不少给模型证据。
- `_cap_matches` 复刻了旧实现一个反直觉口径:命中数**刚好**等于上限时 `truncated` 取决于
  "最后一个命中文件之后还有没有文件",而不是"是否存在第 N+1 条匹配"——等价性用例把这条钉住,
  否则换引擎会静默改变 `truncated` 位。
- 新增 `app/tools/symbols.py`(119 行):`find_symbol(name, kind?)` 定义跳转(精确同名优先,
  无精确命中才给前缀匹配——否则搜 `parse_date` 会被 `parse_datetime` 淹掉;按路径排序、
  `max_symbol_results` 裁顶)+ `describe_file(path)` 单文件大纲(顶层类/函数 + 类内方法、
  真实行区间、签名;解析失败是**可用结果**,带 `parse_error` 而不是报错)。
  边界与 `read_file` 同口径:`relpath_within` 拒越界、`looks_like_text` 拒二进制、
  符号链接条目直接跳过(解析一次指向仓库外的软链就等于把外部文件的大纲引进来)。
- `app/tools/files.py`:`read_file` 增 `limit`,与 `ctx.max_read_lines` **取小**——天花板由 Settings
  定,模型传更大的数只会拿到更少;不传 limit 时逐字同旧行为。`search_code` 增
  `context_lines`(夹扣到 `max_search_context_lines=5`,不报错,免得模型为多要 5 行而整查询失败)、
  `per_file_cap`、`regex`(非法模式 → 失败文案点名该模式,模型能自纠)。
- 两臂对照同步:`app/evals/single_shot.py` 的 `ONE_SHOT_TOOLS` 一并加上两个新工具。
  消融变量是"有无测试反馈/有无重试",不是"有无检索工具"——少一边给一边就把对照臂做成残臂
  ([[ablation-experiment-parity]])。
- 新 Settings 三键 `search_engine`/`max_search_context_lines`/`max_symbol_results` 全部进
  provenance `SNAPSHOT_KEYS`(它们决定模型每轮检索看到什么内容),`tests/test_driver.py`
  的字面键集合同步扩列。
- 复核时发现的**新缺陷**已量化并拆卡:见 D.12(500 条清单上限把搜索遍历域也裁掉了,
  9 题里 4 题的必改文件落在可见范围外)→ M3.6 / M2.5。
- 两处不如规矩的地方,如实记:① 子代理生成了 826 行的 `tests/test_search_tools.py`,
  超 AGENTS.md「单次生成不超过 300 行」的上限(代码文件本身没超),覆盖是好的,
  体积是违规——不重写以免掉覆盖,但记在这里;② 该卡在 150 个子代理回合上被打断,
  中断点是 rg 预筛的重构,我复核时它已落到"显式文件参数"这条正确路线上,故直接续验未回滚。
- 证据(本条 commit 前跑):`ruff check .` 全过;定向 `148 passed, 1 skipped`
  (skip 是 Windows 无创建符号链接权限,符号链接拒绝用例在 Linux/CI 才真跑);
  全量见 commit 前的 `pytest -q` 汇总行。

## M6 状态持久化与中断恢复(2026-10-07,子代理实现 + 主代理复核改两处)

把 checkpointer 从"纯留档"变成"位置权威",并把循环的工作记忆持久化到 turn 边界。
此前的真实代价:跑了 19 轮定位后进程死掉,重启只能从零开始(`recover_stale` 一律判 NEEDS_REVIEW)。

- **A 级** `app/graph/loop_state.py`:`LoopSnapshot`(version/stage/round/turn/messages/
  三个 token 累计/last_content/task_id)`save|load`,落盘在任务自己的 run_dir
  (`loop_state.json`,与 trajectory.jsonl 同处,不新造路径口径)。
  三条硬规矩:①`tmp` + `os.replace` 原子写(崩溃不能留下半份快照被续跑当真);
  ②读侧宁缺勿信——版本/阶段/轮次任一不符或 JSON 损坏一律返回 None(退回旧路径,绝不抛异常);
  ③超过模块上限就**不写**并只记 debug 日志(可选特性不得反过来影响任务)。
  `run_plain_loop` 每个 turn 的**一致点**(工具回执追加之后)写一次;
  `resume_snapshot` 给出时按快照播种 messages/累计量并从 `turn_no+1` 继续,
  **不重置轮次上界**(崩溃不能换来更多 turn 额度);finish/取消后快照不再可续。
- **B 级** `app/graph/resume.py`:`graph.get_state(thread_id)` 读回 `(values, next, metadata)`,
  重建闭包对象(ctx/baseline 按设计不进 state),把 `next` 节点与 A 级快照接上,
  然后 `graph.invoke(None, config)` 从检查点继续——这是全仓第一次真的**读**检查点。
- 两条安全前提写进模块 docstring 并各有用例:
  ①**可写阶段续跑前必须复位工作区到基线**:块协议锚点是拿磁盘实际内容校验的,
    崩溃残留的半截补丁会让续跑的补丁落到错误位置;只读阶段(LOCALIZE/PLAN)刻意不复位,
    否则反而毁掉取证现场;
  ②**门禁必须重跑**:恢复只带回升阶时的工作记忆,绝不带回任何判断——apply 门禁、
    verify 双测试集、E3 双跑复核、最终 verdict 都在续跑那次真实重算(用例数事件)。
- `service.recover_stale` 分两路:graph 引擎 + run_dir 里有**通过校验**的快照 + 抢到既有任务锁
  → 重新入队续跑;其余(快照损坏/版本不符/plain 引擎/自定义仓库任务题面无法重建/抢锁失败)
  → 与分流引入前逐字相同地判 NEEDS_REVIEW。返回值语义("处理掉的僵尸行数")保持两路都算。
  双恢复拦截用的是 `app/storage/locks.py` 既有锁,且刻意**不做 force_release**
  (强清锁正是"两个进程同时续跑同一任务"的入口)。
- 主代理复核改掉的**两处**:
  ①原实现 `recursion_limit = 原上限 + 已烧步数`,等于给崩溃的任务多发循环额度——
    改为只顺延**剩余额度**(`原上限 - 已烧步数`),已烧完则拒绝续跑;
  ②顺序缺陷:原实现先复位工作区、后判额度,于是一个最终被拒绝的续跑已经先把崩溃现场抹掉了。
    额度判定挪到函数开头,在任何 mutation 之前。补了用例断言"拒绝续跑不动工作区"。
- 已知限制如实写进代码与文档(不是"看起来能续"):plain 引擎无图检查点不参与 B 级恢复;
  自定义仓库任务的测试集不在 tasks 表里(issue_text 还截到 500 字符),无法忠实重建 → 判死。
- 文档同步:`app/graph/checkpoint.py` 自述、ADR-0001、`docs/design.md` 里"checkpointer 仅留档、
  不提供崩溃恢复"的声称已改为实际语义;顺手补掉 M5 留下的文档债(recursion_limit 4N+8 → 5N+8、
  转移表补 plan 一步)。历史审计文档(`interview-audit-2026-09-24` 记的当时数字)**不改**——
  那是时间戳证据,不是当前声称。
- 体积不如规矩:`tests/test_resume.py` 611 行、M3 的 `test_search_tools.py` 826 行,都超
  AGENTS.md「单次生成 ≤ 300 行」。覆盖本身是有用的(恢复这类路径不铺用例等于没做),
  但体积违规如实记在这里,拆分留给后续人工整理,不为了合规临时删覆盖。
- 证据:定向 `29 passed`(loop_state + resume + docs 锚点)与服务/图路径在 M6 自测中通过;
  M6+M1.5 合并 commit(`62ced9c`)之后的全量 **`687 passed, 3 skipped`(15:37,exit 0)**
  ——开工基线 536 → 687,净增 151 例,skip 仍是 `test_locks.py` 的两条无 Redis 常态 +
  一条 Windows 无符号链接权限。

## M7 收口:生产默认、测试隔离、ADR 与文档债(2026-10-08 凌晨)

- **M1.5 落地——压缩不再默认关闭**:`Settings.context_window_tokens` 从 `0` 改为 `16_000`。
  默认 0 等于机制没上线,而"上下文只增不减"是有付费证据的主死因,留着关闭默认就是留着那个缺陷。
  推导写进 config 注释:一条折叠后的 `read_file` 回执约 800-1000 tokens,
  `context_keep_recent_turns=6` 的不可压尾部约 6-7k,16k 意味着"超出在用尾巴约 9k 的历史"才让位。
  这是**工程判断,不是实测结论**(FakeLLM 无视消息内容,零成本回放照不出压缩对真实模型的效果,
  ADR-0003/0007 已把这条边界写死),要证明"提高解决率"必须真实模型批 → 待用户裁决。
- 顺带修掉一处会让两臂不可比的写法:`run_plain_loop` 的阈值参数从 `int = 0` 改为
  `int | None = None`(None = 跟随 Settings,与既有 `token_budget` 同一约定)。
  原写法下 graph 臂由 nodes 显式传 Settings 值、而 plain/消融臂按签名默认拿到 0,
  等于"一边压缩一边不压缩"这种**未登记的消融变量**——正是本仓库作废过一次付费实验的那类缺陷。
  配套改两条用例:①`test_loop_context_defaults_follow_settings`(默认必须是 None,Settings 默认 16000);
  ②`test_loop_context_window_zero_reproduces_old_message_sequence` 改为**显式传 0**——
  "关"现在是需要显式选择的档位,不能再借"默认值"的名义。
- **测试隔离缺陷修复**(全量套件里复现过一次的假失败):`tests/test_resume.py` 的
  `recover_stale` 用例会真的把续跑任务 submit 到线程池,用例断言完"已重新入队"就返回,
  于是那次真实图执行在后台一直跑,和后续用例抢 `get_settings()` 缓存
  (conftest 早记过同一类窗口),把 `tests/test_auth.py::test_report_trajectory_cancel_protected`
  打成只在整套顺序下复现的失败。补 autouse `_drain_services` fixture:每个用例结束统一
  `service.shutdown()`。定向复现顺序无法触发(必须整套跑才暴露),因此这条也写进记忆。
- ADR 补齐(0004 上下文分层 / 0005 检索引擎 / 0006 崩溃恢复 / 0007 计划工件),
  每篇都带"反方"与"未证明"条目;`docs/README.md` 索引从"三篇(0001–0003)"改为"七篇(0001–0007)"。
  踩到并修好一条仓库既有机制:`tests/test_docs_anchors.py` 会解析每篇 ADR 的「## 验证锚点」节,
  要求**严格形状** `- \`路径:行号\` — \`子串\`` 且逐条验真(行号必须存在、子串必须在该行)。
  我第一版按散文写锚点,直接被这两条用例拒收——这是防文档漂移的正面证据,不是阻碍。
- 文档债清掉:`docs/adr/0001` 与 `docs/design.md` 的 `recursion_limit` 声称 4N+8 → 5N+8、
  转移表补 `plan` 一步;`app/graph/checkpoint.py`、ADR-0001、design.md 三处
  "checkpointer 仅留档、不提供崩溃恢复"的旧声称改成实际语义(M6 后事实变了才改文案,
  历史审计文档 `interview-audit-*` 不动——那是时间戳证据而非当前主张)。
## M8 接线级行为证据(2026-10-08 00:40)

- 收口后自查发现两处"机制在,链路没证":压缩只在 `run_plain_loop` 直调下测过、
  骨架进 PLAN 只有源码级断言(`extra_system=` 三处)。**新增 `tests/test_context_wiring.py` 4 例**
  把这两条补成行为证据:①小阈值下压缩真的发生、`context_compact` 事件带 `stubbed>0` 且循环仍能
  `finish`;②跑真图时 PLAN 那次请求的第一条 system 里同时有 `<repo_skeleton>` 和真符号
  `def parse_date(`(计划段要能点名要改谁);③`repo_map_enabled/context_window_tokens/
  plan_stage_enabled/loop_snapshot_enabled` 四个开关同时关闭 ⇒ `verdict=resolved` 与
  `changed_files=['src/dateparse.py']` 与引入前同形;④`PLAN_MARKER` 由提示词与 FakeLLM
  共用一个常量(两处各写字面量迟早漂移,这条是防自己)。
- 阈值仍是用例里显式调小(1500),不是 16000:fake 语料每题 7-9 轮、够不到生产默认,
  **所以这条证据只支撑"链路通",不支撑"默认值省了多少额度"** —— 后者要真实模型批。
- 写这卡时自己踩了两个低级错并被工具当场抓住:断言字符串里嵌了裸双引号(语法错)、
  以及留了一条无意义的占位断言(`block(TARGET, ["placeholder"])`)和一行假 monkeypatch ——
  复核自己的测试文件和复核子代理的文件一样必要。
- 证据:本卡 4 例绿;全量复跑 **`691 passed, 3 skipped`(15:30,exit 0)**(前值 687 + 本卡 4 例)。
  **(M10 复盘作废一半:其中压缩那 1 例的夹具当时不是 git 工作区,绿得依赖 `--basetemp`
  恰好落在本仓库的 gitignored 目录里;已改成真 git 工作区并补 `patch_applied is False`。
  其余 3 例与那条 691 的全量数字不受影响——它们跑的是真图路径。)

## M7.1 零成本回放对照(2026-10-08 00:36,唯一还没做完的证据项已补上)

- 跑法:`PATCHPILOT_LLM_ENABLED=false`,graph 引擎逐题 `run_single --model fake --engine graph`,
  35 题 → `runs/night-final-2026-10-08`,**resolved=35 not_resolved=0**;
  基线 = `runs/graph35-v2`(锚 `8ab4e08→d50f251` 那批)。对照工具 = 本夜新写的
  `scripts/compare_batches.py ... --tools`(判定字段不等即退出码 1)。
- 结果:**判定字段逐题 0 条差异**(35 题的 status/verdict/rounds/gate_violations/changed_files/
  baseline_*/verify_* 全等),工具退出码 0。唯一差异全在成本档且可解释:
  `合计 turns 249 → 284`、`tokens 6917 → 7445`(+7.6%),逐题都是 `turns +1`、
  `tools/PLAN: llm 0→1, finish 0→1`——**多出来的正是 M5 的计划段那一次调用**,
  每题每轮 +1 次请求,这是计划工件的代价,不是抖动。
- `context_compact` 事件 **0 次**:fake 语料每题只有 7-9 轮、上下文远不到 16k 阈值,
  所以这条回放**只证明压缩不干扰既有判定路径,不证明压缩省了多少**——省额度/提解决率
  需要真实模型批(待授权),这句话在 ADR-0004 的反方条目里也写着。
- 因此本夜对"效果"的全部可主张部分仍然是:**接线正确、门禁零放松、判定零回退**;
  修复率与 token 效率的主张一条都没提,也提不出。

## M7.1 的口径提醒(写给下一个人)

- FakeLLM 无视消息内容(ADR-0003 自认),所以"改了提示词/加了骨架/加了计划段"在零成本回放里
  **永远不会**表现为 verdict 或工具调用次数的变化——它只验证管道不坏。
  谁要是拿 fake 批的 `avg_tokens` 或 resolved 数当机制证据,就是把 plumbing 当 capability。
- **仍开放(不自行推进)**:压缩/骨架/计划段对真实模型修复率的影响需真实批次(付费,待授权);  `context_window_tokens` 阈值的实测标定;`nodes.py` 已达 1188 行的拆分(禁区文件大重构不当夜做);
  `@`/`=` 测试 id 白名单、`metrics.py` 的 gold 结构对比、`BUG-014` reference.diff 存量缺陷照旧待裁决。

## M9 两臂齐证 + 真进程死亡续跑(2026-10-08 01:10)

- **plain 臂补齐**:M7.1 只回了 graph 臂,单测之外还差"旧引擎没被搅动"的证据。
  跑 `runs/night-plain-final-2026-10-08`(HEAD=`b6c6d8e`,worktree 干净)对基线
  `runs/fake35-v2`(`d50f251`),`scripts/compare_batches.py --tools`:
  35 题判定字段**逐题一致**,成本档 `turns 214→214`、`tokens 6225→6225` 连数字都没动,
  唯一差异是 `duration_ms`(逐题 ±0.1~1.7s 的墙钟抖动,最大一档 BUG-027 是基线自己偏慢)。
  两臂合起来才叫对齐:graph 臂 +1 turn 是计划段的真实代价,plain 臂一字不动是"没串台"的证明。
- 如实记两条口径杂音:①基线批 provenance 里 `llm_enabled=true`(那是当时的 Settings 值),
  但 `model_provider=fake-replay` 与每题约 180 token 的量级说明实际打的就是 FakeLLM,
  两臂零花费成立;②对照批的 `config_snapshot` 比基线多 15 个键 —— 14 个是本夜新登记的
  行为键(`provenance.py` 的 `SNAPSHOT_KEYS`),`localize_budget_share` 是既有语义键补进快照。
  这是"改动被记账"的样子,不是漏跑。
- **`tests/test_resume_crash.py`(1 例,11.9s)**:M6 的崩溃用例用的是 `BaseException` 代理,
  异常路径会走完 `finally`(连接正常关闭、清理执行)——那不等于进程被杀。
  现在崩溃方跑在 `subprocess` 里、看到本轮历史已有 `apply_patch` 就 `os._exit(7)`,
  父进程断言:退出码 7 且没打印"我没死"、A 级快照已在盘上且停在 PROPOSE 第 1 轮、
  脏补丁(动过锚点行的那份半截补丁)确实落盘;然后**由测试进程**重开同一个
  `checkpoints.sqlite` 续跑,断言 `verdict=resolved`、`resume_reset_workspace` 的
  `rolled_back=True`、复位后 MARKER 消失、续跑这次真有 `apply_gate`/`run_tests`、
  `create_workspace` 在续跑之后 0 次。
- 刻意复用 `test_resume.py` 的 `_partial_diff()`/`MARKER` 技巧:如果工作区没被真的复位,
  续跑那份补丁就会 anchor 不匹配 → "续跑成功"本身即"复位发生过"的证据,不靠事件名自证。
- 稳定性:连跑 3 次均 11.9s 绿(子进程 + 真 git + 真 pytest 子运行,不是 mock)。
  写这卡时第一版断言按"补丁后文件里会有 `date.fromisoformat`"来钉 —— 那是**猜的**,
  `_fix_diff()` 实际加的是 `if not value.strip(): return None`;跑一次就当场红了。
  教训照旧:断言要么来自读过的实现,要么就别写。
- ADR-0006 的验证锚点节加了 `tests/test_resume_crash.py:1`(钉模块 docstring 而不是用例行号:
  锚到 `:84` 时,文件里任何一行增删都会让锚点先漂移再红,行 1 不跟着用例长度跑)。
- **文档漂移顺手钉死**:自查发现 README 的能力清单还是"7 个受控 Agent 工具"、状态链里也没有
  PLAN —— M3 加检索工具、M5 加计划段之后这两句都过期了。回改成 9 个/含 PLAN,并在
  `test_docs_anchors.py` 增一条以 registry 为准绳的逐名对齐用例(条数、名单、顺序全等才算过,
  伪工具 `finish` 不计)。这类"条数与名单"的漂移 file:line 锚点盯不住,只能拿代码当准绳 ——
  锚点机制本夜第一次被自己的盲区绊到,补的是机制不是文案。
- **默认阈值"会咬人"的第 16 例**
  (`test_token_window.py::test_production_default_threshold_bites_on_realistic_shape`):
  M8 已经承认 fake 语料够不到 16k,那 16000 就仍可能是个装饰数字。这条按真实 PROPOSE 轨迹的
  形状造超载(每条 tool 回执 ≈ 窗口的 1/8、20 组,夹具自带 `before > 2×window` 的自检),
  断言压回 `Settings.context_window_tokens` 之内,并同时钉三条"发出去的请求必须合法":
  pinned 头原文不动、最近 `context_keep_recent_turns` 轮原文可见、无孤儿 tool 消息。
  尺寸从 `Settings.model_fields[...]` 的**声明默认值**推导而不是读 `get_settings()` 缓存:
  套件里有用例 monkeypatch 环境变量后 `cache_clear()`,teardown 还原得了 env、还原不了已被
  重建的缓存,顺用缓存就会让这条按用例顺序假红 —— 第一次写出这种"读全局"的测试就撞上它。
  它证明的是"默认值不是装饰",依旧不是"省了多少额度"。






## M10 实测替换推算 + 一条假通过的自查(2026-10-08 01:30)

全量套件跑出一红一 flaky(853.89s,`2 failed, 690 passed, 3 skipped`),两条都追到底:

- **`test_bugset.py::test_bug_baseline[BUG-015]` 红:先归错,后更正。**
  嵌套 pytest 不接 `--basetemp`,所以它用机器默认根 `C:\Users\25924\AppData\Local\Temp\pytest-of-wabcy`。
  **(M11.5.0 再更正:这句"嵌套 pytest 不接 --basetemp"是错的)** 适配器 `run_pytest` 一直在追加
  `--basetemp=<报告目录>/basetemp`(`app/adapters/pytest_adapter.py:255`),而且这件事
  `docs/postmortems/PM-004-pytest-basetemp-acl.md`(2026-09-16)就写得很清楚 —— 我登记 M11 时
  只读了 `build_pytest_cmd` 没顺着调用方走,也没查 postmortem。真正用机器根的是
  `tests/test_bugset.py:53` 那个**不经适配器的裸 subprocess pytest**(基线校验),
  以及套件自身;`_isolated_nested_temp` 恰好透传 TEMP,所以修法仍然成立,只是因由换了。
  第一次红时我在套件运行期间另开了 5 个独立 pytest 进程,便把账全记在自己的并发上;
  **第二台"干净"的套件(01:32→01:51,期间没有任何会起嵌套 pytest 的并发)同一处又红了**,
  错误形态一模一样(`os.scandir(root)` → `PermissionError [WinError 5]`)。
  所以真正的因由是"所有嵌套执行共用同一个临时根 + 套件自身就有并发路径(候选并行、双跑复核、
  上一用例残留的后台线程)",外层 `--basetemp` 互不相同挡不住它 —— 第一次的归因不完整,在此更正。
  已在测试侧收口(`conftest.py` 的会话级 `_isolated_nested_temp`,把 TEMP/TMP/TMPDIR 指到
  本次会话私有目录;执行器的 env 白名单本来就透传这三个键,故无需动生产代码)。
- **`test_context_wiring.py::test_compaction_engages...` 是 M8 那条用例自己的缺陷,而且是"假通过"。**
  夹具 `_big_repo()` 只写了个普通临时目录,而 `run_plain_loop` 收尾要用 `working_tree_diff`
  判 `patch_applied` → 非 git 目录当场 `GitCmdError: not a git repository`。它当时能绿,
  几乎可以确定是那几次跑批把 basetemp 指在仓库根(`.pytest-tmp*/`,`.gitignore` 第 6 行收着,
  仓库根现在还留着 11 个这样的目录):`git -C <临时目录> add -A -N` 沿父链找到本仓库的 `.git`,
  又被忽略规则挡住加不到东西,于是既不报错、diff 又是空的。
  换句话说**这条用例的绿依赖临时目录的位置**,换到 `D:/tmp` 下就必红(现已独立复现)。
  修法是让夹具成为真 git 工作区(`materialize_repo(..., extra_commit=False)`),并补一条
  `outcome.patch_applied is False`——把那一步真实执行的 git 判定也变成证据的一部分。
  M8 卡里"本卡 4 例绿"的结论据此作废,已在原处标注。
- **`recursion_limit = 5N+8` 的斜率从推算换成实测**(`test_plan_invariants.py` 新增 1 例):
  runner 的注释原来自称"固定前缀 4 步 + 每轮 5 步 + 收尾 1 步"(=5N+5,与实物 5N+8 对不上),
  而既有用例只证明"N=6 时 5N+8 够用",证明不了斜率对。新用例读检查点里的 `metadata.step`,
  在 N=2/3/6 实测消耗 13/18/33 个 superstep —— 严格线性,**每轮 5 步、固定段 3 步**,
  所以余量恒为 5 且与轮数无关(旧公式 4N+8 在 N=6 就已经差 1 步,与既有那条炸
  `GraphRecursionError` 的用例互洽)。拓扑变动会先把这条钉红,而不是等长重试任务在生产里炸。
  runner.py 里那段注释同步改成实测口径,**行为零改动**(禁区文件只动注释)。
- **同类缺陷排查 + 机制化防线**:按"会碰 git 的入口"清点 —— `run_plain_loop` 在 tests 里的
  20 个调用点传的 workspace 全部来自 `create_workspace`/`materialize_repo`,裸临时目录只有
  M8 那一处;仓库根也没被这些用例写脏(无 stray `unused.jsonl`,`git status` 无残留 staged 项)。
  防线落在 `tests/conftest.py`:session 级 autouse 断言 basetemp **不得**位于仓库内,违反就
  当场整体报错,而不是让某些用例悄悄变绿(CI 用默认 basetemp=系统临时目录,不受影响)。
- **新发现(只登记,不擅改禁区)M11**:被诊断仓库的测试用的临时根没有被隔离 ——
  `app/adapters/pytest_adapter.py:113` 造的命令不带 `--basetemp`,执行器也没换子进程的
  TEMP/TMPDIR,所以所有并发执行的 `tmp_path` 都落在同一个 `<系统临时>/pytest-of-<user>/` 根下,
  而 pytest 只保留最近 3 个编号目录。本夜拿到的 `PermissionError` 只是它的 Windows 侧表现;
  真正要防的是长验证的临时目录被后续执行清理掉 → **假 VERIFY_FAILED**。
  两条修法(命令里加 run 私有 `--basetemp` / 执行器 env 白名单里重定向 TEMP)都动"必须掌握"区,
  按 AGENTS.md 不自行动语义变更,记进 TODO M11.2 等裁决。
- **README 数字全量对账**(照 P3-7 的路子,每条都拿实物数一遍):端点 7(`app/api/routes.py`
  的装饰器计数)、题目 35(`bugs/BUG-*` 目录)、攻击样例 10(`bugs/attacks/*` 目录)、
  隔离实验 5(`docker-isolation-notes.md` 结果表行数)、hard 段 BUG-029..035 存在——
  四条本来就对,错的仍只有工具条数与状态链那两处。把题数/攻击样例数也钉成用例
  (`test_readme_dataset_counts_match_the_fixtures`),因为 R3-Q1 记过的正是
  "样例入库后条数没回改"这一类漂移;数字写死在文案里,就得有人每次对着实物数。
- **M11.4 收口 + 自己制造又当场修掉的一个错**:
  ①第二轮全量(01:32→01:51)期间没有任何会起嵌套 pytest 的并发,`test_bugset[BUG-015]`
  仍以同样的 `PermissionError` 红 → 根因是"所有嵌套执行共用机器默认临时根"本身,不是我的
  并发操作(第一次归因不完整,已更正)。测试侧加会话级 `_isolated_nested_temp`
  (TEMP/TMP/TMPDIR 指向本次会话私有目录;执行器 env 白名单本来就透传这三个键,
  所以不必动生产代码),生产侧缺口仍留在 M11.2 等裁决。
  ②用 `Path.write_text` 批量改 TODO 标题层级时,Windows 下它把每行 LF 写成 CRLF;
  `.gitattributes` 是 `* -text`(故意关掉行尾转换,补丁与 diff 要字节级一致),于是 `git diff --stat`
  显示整文件 208 行全变。`read_bytes().replace(b"\r\n", b"\n").write_bytes()` 复原后
  numstat 回到 72/2。规矩:**本仓库文本改动一律走 Edit 或 write_bytes,不用 write_text**,
  批量改完必查 numstat —— 这类错只有 diff 数字能暴露。

## M12 补上缺的那篇 ADR:补丁协议(2026-10-08 01:59,纯文档)

- 目标里"推荐 Patch/Diff 局部编辑或精确定位替换,避免全文件重写"正是 ACI 层最核心的决定,
  而七篇 ADR 没有一篇写它 —— 只在证据/复盘文档里被引用过。这是文档面的真实缺口,不是锦上添花。
- 新增 `docs/adr/0008-补丁协议-块锚定局部编辑.md`,按仓库既有形状写(背景/候选/结论/后果含反方/验证锚点):
  结论的要点是**块协议编译到 unified diff,而不是替代它** —— `app/gitops/blockpatch.py:1` 自述
  "防线链一个字节都不改";`_locate` 把"模型写错行号"这一整类失败模式在协议层消掉
  (0 命中 `anchor_not_found`、多命中 `ambiguous_anchor`、早于游标 `chunks_out_of_order`);
  `split_keepends` 只按 `\n` 切行,禁 `splitlines()`(它还会在 `\x0b \x0c \x1c-\x1e \x85 \u2028` 切)
  与 `Path.read_text`(universal-newlines 静默把 CRLF 变 LF)—— 与本轮 M10 那个 CRLF 事故同一条教训。
- 反方条目如实写,不含修饰:上下文重复的真实金补丁**无法机械 round-trip**(模型可自纠、转换器不行,
  所以这类题不入集)、rename/copy 与二进制 diff 不迁移、`ambiguous_anchor` 是额外一轮消耗、
  **协议相对裸 diff 的收益没有对照数据**(FakeLLM 照不出协议与提示词差别,要真实模型批才能主张)。
- 锚点 4 条按实际行号写并经 `tests/test_docs_anchors.py`(6 例)逐条验真;`docs/README.md` 索引
  "七篇 → 八篇"。代码零改动。

## M9-M14 的收口数字(2026-10-08 02:43,最终一次全量)

- `ruff check .` + `ruff format --check .` 全绿(184 文件),
  全量 `pytest -q --basetemp=<仓库外>` → **`706 passed, 3 skipped, exit 0`(14:01)** ——
  这一轮跑的就是最终树(含 M13 度量脚本与 M14 的 ast_outline 用例),不是中间态。
- 条数算术自洽:02:19 那轮 696 → +`test_ast_outline.py` 10 例 = 706;
  3 条 skip 是 redis 两例与 Windows 无符号链接创建权限一例。
  696 那轮的算法(M8 时 691 + 真进程死亡 1 + 生产阈值 1 + README 准绳 2 + recursion_limit 1)
  与这一轮互相印证,没有"数字涨了就算证据"的空档。
- 关键是这一轮**没有任何 F**:前两轮都在 `test_bugset[BUG-015]` 上红,修的是
  "嵌套 pytest 共用机器默认临时根"(M11.4 的会话级 TEMP 隔离),同时把 M8 那条位置相关的
  假通过改成真 git 夹具(M10.1)。也就是说:这两轮的红不是"运气不好",是两条真实缺陷的表象。
- 仍未证明的仍是同一批:压缩/骨架/计划段/块协议对真实模型修复率的影响、
  `context_window_tokens` 的实测标定、生产侧每次执行私有临时根(M11.2 等裁决)、
  大纲是否保留参数类型标注(M14.2 等对照)。



## M13 真实语料复查:遍历域解耦到底管不管用(2026-10-08 02:23,零成本)

- 单测只能证明形状,证明不了真实仓库里的位次。新增 `scripts/measure_retrieval_domain.py`,
  拿 `.pytest-tmp/base-SWE-*/repo` 这份**真实上游仓库**的基线缓存复查 D.12 那条缺陷与 M3.6 的修复。
- 实测 12 题可测(9 题无缓存,如实跳过不算分母):必改文件共 **20 个**,
  新遍历域(`max_search_files=8000`)**全部可达**,最大位次 1168,没有一题触到上限;
  其中 **5 个文件在旧的 500 条上限下是不可见的** ——
  sphinx-7590 `sphinx/util/cfamily.py`=619、astropy-8707 `io/fits/card.py`=648 与 `header.py`=666、
  sphinx-9461 `sphinx/util/inspect.py`=675、pylint-6903 `pylint/lint/run.py`=1168。
  也就是说旧缺陷在这 12 题里挡住过 4 题的必改文件(33%)、5 个文件(25%)。
- AST 大纲在真实第三方代码上的表现:**20/20 可解析**,共给出 2263 个符号条目
  (`find_symbol`/`describe_file` 与骨架共用同一套 `ast_outline` 规则,所以这个集合就是模型能看到的集合)。
  这条比"合成夹具上解析得动"强一档:sphinx/astropy/pylint 的大文件里没有一个解析失败。
- 脚本对缓存缺失的行为是**如实报告 0 题**而不是假装通过;它不进测试套件
  (基线缓存是定向用例的临时产物,不该让全量绿依赖它的存在)。

## M14 完成度对账查出的两处账目缺口(2026-10-08 02:27)

- 对账时按"每个模块都要有自己的用例"逐个数文件:`ast_outline.py` **没有对应测试文件** ——
  渲染侧被 `test_repo_map.py` 间接钉着,结构化侧被 `test_search_tools.py` 经工具间接覆盖,
  但这个模块立身的不变量("同一份源码,骨架渲染出的符号集合与工具给出的结构化集合逐项相等")
  没有任何用例直接守。新增 `tests/test_ast_outline.py` 10 例补上:两侧逐项相等(名字+顺序+
  签名+行区间)、局部函数两侧都不出、类嵌套只再进一层(`TooDeep` 不出)、kind 三分、
  装饰器/async/返回标注,以及 `parse_source` 的**"永不抛出"契约**。
  契约那条刻意只断言 `(树为空) ⇔ (有错误串)`,不钉异常名 —— 400 层括号在 3.11 抛
  RecursionError、3.12 抛 SyntaxError,把解释器内部当断言对象,用例就会随 Python 版本红。
- 写这组用例时**被真实行为纠了一次**:我先按直觉断言签名保留参数类型标注
  (`def top(x: str) -> bool`),跑出来是 `def top(x)` —— 移植前的口径就是丢参数标注、
  只留返回标注。已把断言改成事实,并另写一条用例把这条取舍写进注释;
  "是否值得保留参数标注"记进 TODO M14.2 等一次真实两臂对照,不拍脑袋改(那等于改模型
  每次请求看到的东西)。

## M11.5 并发临时根:把一条"听起来对"的风险测成数字(2026-10-08 11:52,零成本)

M11.3 要的是生产影响评估,结果它先推翻了我自己登记的 M11.1:

- **测量结果(判据 (A) 成立)**:两条并发执行只要**共用同一个 report_dir**(basetemp 由
  `junit.parent` 派生,所以就是共用同一个 basetemp),就会互相删对方的临时根 ——
  快版(不 sleep)伪失败 **9/18**,慢版 **7/18**;各自隔离 report_dir 的对照组 **0/18**;
  控制组单独跑都 `all_passed=True`。失败有两副面孔,都会把判定翻成"没修好":
  测试中途 `FileNotFoundError`(先起步那次的 `tmp_path` 文件被后起步者在 session setup 的
  `rm_rf` 删掉)和会话级 `errors=1`(被删的一侧连收集都做不完)。
  **快版比率反而更高** ⇒ 窗口就在"后起步执行 `rm_rf`"那一刻,与测试跑多久无关,
  我登记的放大器不承重 —— 这条按事先写死的规则算数,不算"是放大器造出来的"。
- **今天够不够得着**:主流程 verify 4 次串行、候选按 `cand{i}` 分目录、跨任务按 run_dir 分目录,
  所以没有可达路径;但 `_kill_tree` 自己有注释说超时可能有**逃逸的孙子进程**
  (`local_runner.py:144-156`)—— 孤儿还在写、下一轮在同一个 basetemp 上 `rm_rf`,
  这就是同一任务内的并发共享。所以它是一条**已存在触发路径的潜伏缺陷**,不是纯理论。
  修法要 1 行且必须**每次执行唯一**(不能按 run_dir/report_dir),属禁区语义 → TODO M11.5.5 待裁决。
- **暴露面数出来了**:35 道宿主题里被执行到的测试文件用到 `tmp_path/tempfile` 的只有 1 道
  (就是红过的 BUG-015),12 道容器题的临时根在容器内无关。但平台卖的是"诊断任意本地仓库",
  合成语料的 1/35 不能当成生产暴露面。
- **机器默认根泄漏计数 4 → 4**:走 `run_pytest` 的本地执行一次都没落到 `pytest-of-<user>`,
  证实 M11.5.0 的更正;`icacls %TEMP%\pytest-of-wabcy` 至今仍"拒绝访问"(PM-004 的 ACL 损坏
  从未修过,旁边还留着 `pytest-of-SYSTEM`),这是下一条的背景。

## M11.6 我 M10.5 装的防线,把文档里那条 `pytest -q` 冲掉了(2026-10-08 11:55)

- `pyproject.toml:26` 从 M0 起就是 `addopts = "-ra --basetemp=.pytest-tmp"` —— basetemp
  **在仓库内**;而 M10.5 的会话级防线见到仓库内 basetemp 就整体报错。于是
  `AGENTS.md:35`/`README.md:43` 写的 `pytest -q` 现在**必然失败**,而且是先 `rm_rf` 掉
  `.pytest-tmp`(408 个测试期仓库副本)再报错。实测:仓库内 basetemp → `basetemp 在仓库内`
  整会话 error;仓库外 → 正常绿。
- **为什么一整天没暴露**:我夜里每次全量都显式传了仓库外的 `--basetemp=...`
  (仓库根那 12 个 `.pytest-tmp-*` 目录就是痕迹),把冲突用个人习惯遮住了。
  防线本身没问题(它确实拦住了假通过那一类),问题是**配置与文档没跟着改** ——
  登记一条防线的同时就得改掉被它否定的默认跑法,这步我漏了。
- **不能简单删 pin**:这台机器的系统临时根不可用(PM-004 至今未修),退回默认根实测直接
  `PermissionError`。三条收口方案(推荐项+代价)记在 TODO M11.6.3,其中"以管理员权限清理
  `%TEMP%\pytest-of-*`"动的是机器、影响你其它项目,我不主动做。

## M11.5/M11.6 落地:裁决放行后改的三处(2026-10-08 12:45)

- **临时根改成每次执行私有**(`a127b28`,`app/adapters/pytest_adapter.py`):
  `<报告目录>/<junit 名>.basetemp-<8位随机>` + 执行完自己回收。落地时被实测纠正了两点:
  (i) 给了显式 `--basetemp` 时 pytest **收尾不清理**(`tmpdir.py` 的 finish 只处理"没给
      basetemp"那一支),所以"多一个目录"不是小代价而是攒着 —— 于是加了 `_discard_basetemp`;
  (ii) 旧行为的失败不止"测试中途 `FileNotFoundError`":复现脚本里还跑出
      `FileExistsError`(session setup 建目录时撞车)与清理侧 `PermissionError`
      —— 三种面孔,同一个因由。
- **反向验证做了**:把 `_basetemp_for` monkeypatch 回旧写法
  (`pa._basetemp_for = lambda junit: junit.parent / "basetemp"`),新加的并发用例 18 次里
  **12 次伪失败**;新行为 0 次。不做这一步,"回归用例"可能只是装饰物。
  `tests/test_basetemp_isolation.py` 跑真子进程 —— 被删的是文件系统事实,mock 不出来。
- **跑法收口按方案①**:`pyproject.toml` 的 addopts 不再钉落点(注释里写清为什么不钉),
  `AGENTS.md`/`README.md` 的本地命令改成 `pytest -q --basetemp=D:/tmp/pt` 并说明理由,
  防线报错从裸 `WinError 5` 改成给得出路的文案。现在裸 `pytest -q` 的失败是**清楚的**,
  而且**不再先 `rm_rf` 掉仓库内那 408 个测试期副本**。
- **顺手查出一个默认值,记成 M11.7 但把严重性写回真实水平**:`run_pytest` 的
  `report_path=None` 会把 junit 与临时根落进**工作区**,而 `differ.py:30` 用
  `git add -A -N` 取 diff(未跟踪文件进 diff)—— 我第一版把它写成"范围门禁会被平台产物骗",
  核对后不成立:`.patchpilot_junit.xml` 正好在物化仓库强制注入的忽略清单里
  (`testing.py:19`),临时根又被新加的 `finally` 回收。真实剩下的缺口很窄
  (平台进程在 pytest 期间死掉 ⇒ 回收没跑、目录里有文件)。签名要不要改成必填属于另一次裁决,
  我倾向不动,只在文档里写清"默认值仅供测试"。
- **全量确认**:改完之后 `pytest -q --basetemp=D:/tmp/pt-full-m116b` →
  **708 passed / 3 skipped**(1229.21s;比上一轮多出的 2 例就是 `test_basetemp_isolation.py`),
  `ruff check .` 全绿,工作区无残留。
- **ACL 我这边做不到,已探到底**:`pytest-of-wabcy` 能 `dir`(空目录)但 `icacls`/`ren`/
  `icacls /grant` 一律"拒绝访问" —— 当前令牌连安全描述符都读不到,没有 WRITE_DAC 也没有 DELETE。
  需要管理员终端执行,命令写在 TODO M11.6.5。`pytest-of-SYSTEM` 是当年 SYSTEM 身份跑 pytest 的残留,
  pytest 按 `getuser()` 取目录名,我们的运行永远不碰它。
- **配置搬家会把工具晃倒**:`measure_retrieval_domain.py` 原来只扫 `.pytest-tmp/`,
  落点搬到仓库外之后它会"如实报 0 题"——**这是一种假干净**(读起来像没缓存,其实是找错地方)。
  现在默认扫所有 `.pytest-tmp*` 并支持 `--root=`,报 0 时把扫过的落点打出来。
  复跑数字与 M13 逐项一致(12 个仓库、20 个必改文件、旧上限下不可见 5、位次最大 1168、AST 20/20)。
- **测量产物入库时我先误判了一次原因**:把脚本在 13:20 重跑,共享组居然 0/18。第一反应是
  "我刚才在跑测试给它加了负载,污染了" —— 不对,**真正原因是修复已经在代码里**:脚本调的就是
  `run_pytest`,而落键已改成每次执行私有。所以入库两份并写清出处:
  `docs/evidence/2026-10-08-basetemp-contention-prefix.txt`(修复前,共享组 9/18,原文件是
  控制台 GBK 落盘、`iconv` 无损转 UTF-8,数字与文本一字未改)与 `...-postfix.txt`(修复后两臂 0/18)。
  教训:**同一个测量脚本在修复后重跑,量的已经不是同一个东西** —— 留档要留"当时那一版代码"的输出。

## M19 摘要链扫一遍:又找到一处"无声丢失",外加数字与文档订正(2026-10-08 20:10,零成本)

- **修完一处缺陷就把同一族扫到底**:M16.9 的根因是"手工 `tracker.record` 绕过了统一摘要器",
  所以扫了全部手工记录点,再对**全语料轨迹**做"每个工具 `output_summary` 的键数直方图",
  看谁恰好压在 `_summarize_output` 的 8 键上限上。**当场就抓到一个**:`run_tests` 的 941 条事件
  **全部正好 8 键**(源是 `execution.py:50` 那八个字段)。今天不丢东西,但任何工具加第 9 个键
  就会静默从证据链消失且**不留标记** —— 与 `[:200]` 那条同族。
  已写进 `docs/design.md` §8 的已知边界并登记 TODO M19.1,**属 `app/tools/` 边界校验,没动代码**;
  三个裁决选项也写好了(补省略标记 / 抬上限 / 只登记)。
- **"恰好等于上限"是一个可复用的检查动作**:直方图里任何工具压在天花板值上,都说明天花板正在起作用
  而没人注意到——这次就是靠它发现 `[:200]` 同族的第二个实例的(上一条 `[:200]` 是靠读代码找到的)。
- **这条随后就修了(20:50,用户「继续」= 放行推荐项 (a))**:`_summarize_output` 超 8 键时写
  `_omitted_keys: "+N keys omitted"`,8 键以内一字不动 ⇒ **现有轨迹形状零变化**(`run_tests` 正好 8 键)。
  实现前先把用例写红(`KeyError: '_omitted_keys'`)再改,是这一族唯一需要的正向验证。
  顺带补的是**测试盲区**:`_summarize_input` / `_summarize_output` 此前在 tests/ 里零直接用例
  (grep 全库零命中),M16.9 的修法也只有一条用例守着 ⇒ 新增 `tests/test_trajectory_summary.py` 6 例。
  残留边界照实在 design.md §8:300 字符上限只保证**长度**可复原,不保证全文可见。
- **对外数字全部重测后订正**:`708 → 713 passed / 3 skipped` 两处(标题行与"项目一句话"),
  墙钟从 1229s 改成 19:55 那轮的 **1321s**;`docs/README.md` 的 evidence/ 描述从"两份"补到"三份"
  (M16 的补丁段反事实一直没进索引)。**历史 run 的数字不回溯改**(M15/M16 卡里的 706/708 是当时读数)。
- ADR-0004 补了 M17 的**顺序**理由:换算乘的是"压缩之后"的估算,所以"压缩排在门禁之前"仍成立;
  挪到压缩之前,"能压下来也照旧死"就回来了。
- **清理台账如实**:删掉 `D:/tmp` 下历次会话 35 个 + 本会话 20 个测试临时根,清后 `D:` 67% 已用 /
  193G 可用;**清理前没记读数,所以不给"92%→67%"那种前后对比**。
  `D:/tmp/pt` 与仓库根 `.pytest-tmp*` 保留(后者是 `measure_retrieval_domain.py` 的数据源)。

## M17/M18 放行批:门禁的单位、取证的长度、以及下一批先量噪声(2026-10-08 19:40)

- **M17 的成因不是"阈值设小了",是"两个单位加在同一条比较里"**:门禁比
  `provider 真值累计 + 本地估算的待发请求`。M15 量出估算低估 1.47 倍,M16 又量出补丁段那 2 次
  判死都发生在**记录之外的下一次请求** —— 两条凑在一起就是"系统性晚判死"。
  落地:`Settings.token_estimate_factor`(默认 **1.0**),只乘待发那一次,真值累计一个字不乘。
- **为什么默认 1.0 而不是直接把实测的 1.47 写进去**(这条是本卡唯一有争议的设计决定):
  ①历史读数与 M15 的 V1 锚点吃的是未换算估算值,回溯改它等于把已入库的证据换成另一种口径;
  ②1.47 属于"这台模型 + 这批仓库",拿它当平台默认就是我在替 Q3 拍脑袋;
  ③这个字段真正的价值是把"统一口径"变成一件**可以对照**的事。用例①(`默认读数 = 真值 + 未换算估算`)
  就是钉死这条决定的不变量,它同时是"旧行为逐字不动"的回归保护。
- **注册键的连锁义务**:`token_estimate_factor` 决定"闸门在哪次请求判死"⇒ 同题 1.0/1.47 的轨迹
  与终态不可比 ⇒ 必须进 `SNAPSHOT_KEYS`。`tests/test_driver.py` 里那条"每个 Settings 键三选一"
  的用例在这次改动中**先红后绿**——它就是为了防"新键悄悄漏出白名单"而存在(审计 E2 那回漏了
  `verify_double_run`,11 分钟没人发现)。这类"加一个配置键要同步改三处"的账,能由测试逼着改
  比自己记得住可靠。
- **M16.9 顺手还掉的取证债**:`finish` 的 summary 原先在调用点 `[:200]` 无声截断,而
  `Tracker.record` 不做二次摘要 ⇒ 长度当场消失。真正的缺陷不是"截得太狠"而是**绕过了统一摘要器**
  (`registry.py:248` 与 `plain_loop.py:93` 都留了 `... (N chars)`,只有这里没留)。
  改法是删掉调用点的自截断、走同一条 `>300 + 长度标记` 规则;用例两例,反向验证是"改回旧写法
  `[500]` 那例必红"。**旧语料那 7 个下界不因此变精确** —— 它们是在旧口径下录的。
- **这条改动不能拿零成本回放当证据(先把它拒了)**:我本来打算跑 graph 35 题 fake 批次来证明
  "默认 1.0 行为未变",但基线批次每题只烧约 7k tokens,**离任何额度闸都差两个数量级** ——
  跑出来的 0 差异只证明"这条路径没被走到",不证明行为等价。证据是 M17.3 那三条直接对撞
  闸门算术的用例。额度类主张只能来自真实模型语料或显式构造的用例,这条口径与 M15/M16 一致。
- **M18:下一批付费实验先买噪声,不买分数**(`docs/paid-batch-preregistration-2026-10-08.md`,
  状态未启动)。设计依据全在已入库的证据里:同题两次同参运行死在**不同阶段**
  (`swe-hard-graph2` vs `graph3`),而要检测的效应静态上界只有 6% ⇒ **不知道 σ 之前,
  任何两臂差值都不可解读**。所以 Q0 是 7 题 × 3 次同参重复,判据写死"可判别题数 k≤1 就停止付费";
  Q1 用逐题多数票与配对差值(每臂 n=3,不再 n=1);Q2/Q3 的主指标是 tokens p50 与判死率,
  **不是** resolved —— 拿修复率去检验一个 6% 的效应,就是花可测不出来的钱。
  还照实登记了一件事:**M4 的失败反思没有开关**,所以它在本批不做对照,要测得先加配置。
- 本批**零模型调用、零付费**;所有数字要么来自已入库语料,要么是待验证的设计参数。

## M16 补丁段反事实:两个"未覆盖"的判死补上了,顺带抓出自己一个口径错误(2026-10-08 18:26)

- **难点不在压缩而在头部**:补丁阶段的 user 是 `PROPOSE_PROMPT.format(round_no, issue_text,
  findings, feedback)`,`findings` 是定位阶段的产物。轨迹里 `finish` 的 summary 被 `[:200]`
  **无声截断、连长度都不留** ⇒ 7 个"定位成功"的补丁实例头部有不可知常量,只能报下界;
  而**降级**路径留下了 `localize_degraded.findings_chars` —— 恰好就是补丁段额度判死的那两个 run。
  于是这一段的结论只有建立在"逐字节重建成功"之上才作数:A-1 锚给出 63 / 397 **逐字节相同**,
  A-2 锚给出 19,069 vs 代码 19,188、22,946 vs 23,117(0.6~0.7%,零换算)。
- **版本判定不用白名单**:`58472254` 的降级 findings 就是 `last_content.strip()`,
  `293cd649` 起才有 `_provisional_findings()` 的"已调查线索"清单。脚本是**去那版源码里探有没
  有这个函数**,不是列 commit 名单 —— M15.1 刚为"钉死一个 commit"付过一次账,不长第二次。
- **我自己抓出来的口径错误(这条最该记住)**:这两次死都发生在**记录之外的下一次请求**
  (闸门读数是"累计已耗 + 待发那一次")。我前一版按"记录内的回合"判存活,graph2 因此显示成
  "`-→活`" —— 把真实死法整个漏掉了。改成"累计+待发"读数之后,OFF 臂**两次都判撞**,与真实一致。
  ⇒ 判"反事实机器可信"的依据从来不是"它算得好看",而是**它能复现待检验的那次失败**。
- **结果(16000/keep=6)**:graph3(11 回合、findings 397)累计额度省 **15%**、首次触发第 8 回合、
  **撞→活**;graph2(10 回合、findings 63)省 **7%**、**撞→仍撞**。
  所以补丁段这 2 次额度判死是"一次能活、一次仍死",n=2 且动作序列固定 ——
  它只回答"额度余量从哪来",不回答"结果会不会变好"。
- **仍未覆盖(照实写)**:one-shot 臂 `PROPOSE_ONE_SHOT` 8 实例(提示构造在
  `app/evals/single_shot.py` 里,另一套路子)、7 个 findings 被截断的补丁实例(只作下界)、
  以及 round≥2 的 `feedback` 文本(没单独入库,无从重建)。
- **那条取证完整性的欠账已经还了**(2026-10-08 19:20,用户「全部允许」):完整经过记在上面的
  M17/M18 节,这里只留结论 —— **M16 的数字一个没动**:25 份语料是在旧口径(`summary[:200]` 无声截断)
  下录的,那 7 个补丁实例仍是下界(见 TODO M16.9/M17.5)。
- 复现:`PYTHONPATH=. .venv/Scripts/python.exe scripts/measure_propose_counterfactual.py`
  (A-1/A-2 任一未全中即退出码 1);原始输出 `docs/evidence/2026-10-08-propose-counterfactual.txt`。

## M15 上下文机制的额度效果第一次被量成数字(2026-10-08 13:35,零成本、不起模型)

账目见 TODO 的 M15 卡,这里只记方法与"哪些主张因此可以写、哪些还是不能写"。

- **素材一直是空的**:M1.5/M8.1/ADR-0004 都写着"fake 语料只有 7-9 轮,够不到 16000 阈值,
  所以省多少额度不主张"。这句是对的,但它遮住了另一件事——`runs/swe-*` 有 **25 份真实模型 run**
  (367 个 llm 回合,逐轮带 provider 真值与工具全文),跑在**六个不同 commit** 上,
  provenance 里没有 `context_window_tokens`、轨迹里 `context_compact` 事件 **0 次**
  ⇒ 压缩从未介入,正是一批干净的"前对照"。这批真实数据一次都没被拿来做上下文测量。
- **先证明尺子再量东西**(判据写死在 `scripts/context_replay.py` 头部,跑前定死):
  F1 逐 run 守恒 24/25 逐字相等(那 1 条例外是 `4a4093e` 修过的"崩溃路径把用量记成 0"实物,
  单独归类,既不算装载失败也不算通过)、
  F2 动作序列 0/251 错配(251 = 被重放的 LOCALIZE 回合,全语料 367 回合)、F3 R²=0.9424 / 中位误差 13.4% ⇒ 重建可用于点估计。
  A2 又加了一道**零换算锚点**:额度型判死的 error 里 `agent loop tokens X exceed budget Y`
  的 X 减掉已耗真值,就是代码当时算出的**同一口径**估算值 —— 我的重建给出 29,426 vs 代码 29,679
  与 23,638 vs 23,838(都差 0.8~0.9%);**负对照**(故意丢掉工具回执)只给出 1,613 / 1,411,
  差 18~20 倍,所以这个锚真的有牙齿,不是"怎么都能过"。
- **量出来的第一件事实:`len//4` 不是中立单位**。逐实例 real/est 中位 **1.470**(1.263~1.897,n=25)
  ⇒ 生产阈值 16000 对应的真实上下文是 **≈23,500 tokens**。顺带看清 `plain_loop.py:305` 是
  **混单位门禁**(累计用真值、待发请求用估算),它因此系统性**晚**判死;误差只有一个请求的量级,
  不随轮数累积 —— 要不要统一口径属于裁决,不在本卡擅改。
- **量出来的第二件事实:16000 这根杠杆现在几乎不动**。生产默认在 11/25 个实例触发(首次中位第 12 回合),
  触发实例的累计额度节省中位 **6%**(全体中位 0%),省得多的只有最长那几个 run(18~26%)。
  要省到 31% 得同时把阈值降到 8000 且 keep 收到 4,可那里 **52% 的压缩回合压不到阈值**
  (keep=6 是 86%、keep=8 是 100%;分母取"调用过压缩的回合",含"压到底也没变小"的那些 ——
  钉住的最近 N 回合本身就比阈值大),
  ⇒ **阈值与"保留回合数"是联动的**,单独讨论 16000 没有意义。
- **量出来的第三件事最省钱,也最容易被自己读反**:11 次 BUDGET_EXCEEDED 按 error 原文拆开是
  `localize/token-gate` 2 + `propose/token-gate` 2 + `one_shot/token-gate` 1 + **`one_shot/max-turns` 6**。
  那 6 次的约束是**回合数**,而且**全部落在 `runs/swe-hard-oneshot` —— 就是我在 `f4a128e` 作废过的那批**
  (私加轮次上限,0/7 空结果)。排除作废批之后,**可用样本里的 5 次判死全都是额度闸**
  (定位段 2 / 补丁段 2 / one-shot 段 1)⇒ 压缩与预算正是该测的杠杆,而 n=5 也再次说明
  "抬预算赌一次"仍然不是设计。两件事一起记:**status 名不等于死因**(11 次里 55% 压根不是 token 问题),
  以及**作废批必须整体排除、不参与统计**(分母从 11 缩到 5 是口径修正,不是挑样本 —— 判据在拆之前就写死了)。
- **锚点安全性(ADR-0004 那笔没验过的账)**:拿每题 `expected/reference.diff` 的文件清单,
  对固定分母 42 个"不压缩时全文可见"的项比较压缩后的可见性:16000/keep=6 → 41 全文 / 0 存根 /
  **1 连路径都没了**;8000/keep=6 → 38 / 1 / 3。当年"摘要可能丢掉补丁要的锚点"这条顾虑
  在生产阈值上落地为 1/42,**不是零**。另外 12 个压缩单元格(4 阈值 × 3 keep)累计 **0 条孤儿 tool 消息**,
  即压缩后的请求形态在真实轨迹上始终合法(这恰好是曾经付过费的那类故障:`4a4093e` 的 400)。
- **仍然不能写的**:以上全是**额度与形态**证据。静态反事实假设动作序列不变,真实模型看到更短上下文
  可能少查也可能重复查;PROPOSE 段的 2 次额度判死没进反事实(该段头部要按当时版本重建 findings 与
  反馈,风险大,我如实只覆盖 LOCALIZE,并列未覆盖的 9 次);**修复率一类主张仍只能来自真实两臂批**。
  换句话说:M15 把"该花多少钱、值不值"测清楚了,没有把"效果"测出来。
- **闸门红过三次,所以它不是装饰**(记下来,因为"自检通过"这种话只有配上失败史才可信):
  ①第一版分组漏了"新回合开始时先结转上一回合",所有工具回执都没进消息列表 —— 表上表现为
  "上下文从头部到末回合几乎不涨"(1,577 → 1,630),一眼假;修完才涨到 16k~29k。
  ②V1 第一次跑命中率 2/11=18%,原因是我按"纯真值"复刻门禁,而**代码本身是混单位的**
  (累计真值 + 待发请求的 `len//4` 估算)—— 这次"我的复刻不过关"恰好把那条真实缺陷量了出来。
  ③"压不到阈值"一开始算出 102%,分母用错了(用了"变小了的回合",漏掉"压到底也没变小"的),
  换成"调用过压缩的回合"后是 86%/52%/100%。三条都写在脚本注释或本卡里,不靠记忆。
- **来源假设错过一次,是回头复查时抓到的(不是我一开始就查对了)**:我起初当"25 份 run 全跑在
  `80981f0`"这一个 commit 上,并按它取提示词模板 —— 实际读 provenance 一看是**六个 commit**
  (`3f17a9cd`/`1a2646e2`/`80981f04`/`58472254`/`293cd649`/`66e6e309`)。修法不是解释,是改代码:
  脚本改成**逐 run 按自己的 provenance 取头部**,并加 F0 自检把三段提示词的 sha256 打出来 ——
  六个 commit 的指纹全等于 `a965075e7c25`,fold 函数与 `refine_head/tail=40/15`、`hard_cap=8000`
  也逐字节相同,加上 25/25 都是非思考模式(没有 `reasoning_content` 这条隐藏字段),
  ⇒ **已提交的数字一个都没动**(A1/A2 复跑逐格相同)。
  记这条的理由:如果我只在文档里把"八个字的前提"改对而不补 F0,下一次同样会错;
  而且"结论没变"不等于"推理没错"—— 这次结论侥幸没变,是因为那些批次恰好没改提示词。
- **一行复现**(依赖 `runs/`,不入库,只能在这台机器上跑):
  `PYTHONPATH=. .venv/Scripts/python.exe scripts/context_replay.py` 与
  `PYTHONPATH=. .venv/Scripts/python.exe scripts/measure_context_counterfactual.py`
  (退出码:前者 F1 不守恒即 1,后者 V1 锚点未全中即 1);原始输出已入
  `docs/evidence/2026-10-08-context-corpus-calibration.txt` 与 `...-context-counterfactual.txt`。

## S00 实施基线与契约 ADR(2026-10-08,spec《PatchPilot执行SPEC-2026-10-08》开工卡)

接手 spec 执行。**执行根偏离已声明**:spec 书写根是 codex worktree(C 侧),实际在 D 侧
(用户会话目录、.venv 与历史 runs 所在地)执行——两侧 HEAD 逐字同为 `442cd8d`、D 侧工作树
clean,满足 spec"其他 checkout 先核对代码身份"的前提;新增分支
`codex/patchpilot-reliability-20261008` 承载全部实施 commit,master 不动,C 侧零写入
(声明见 docs/spec-s00-baseline-2026-10-08.md §0)。

- **全量离线 pytest 基线**(basetemp=D:/tmp/pt-patchpilot-spec-20261008,仓库外):
  首跑 **2 failed / 717 passed / 3 skipped / 1108.5s**。2 个失败全是 `test_docs_anchors`
  对**本卡新入库的 ADR-0009** 的锚点格式断言(我按散文写了"验证锚点",测试要求
  `- \`path:line\` — \`substring\`` 逐条格式),不是产品失败;按格式修好后复跑该文件 6 passed。
  产品用例零失败。3 个 skip 全环境性(redis×2、Windows 符号链接权限×1)。
  收集数 **722**(审查材料记 710,同 commit 差异未逐例归因,以本侧 collect-only 为准)。
- **35+35 FakeLLM 基线批**(新目录,历史 runs 未动):plain 走 driver CLI
  (`runs/spec-baseline-plain-20261008`,manifest:35/35 resolved,commit 442cd8d,
  worktree_dirty=false);graph 的批 CLI 不存在,按 spec 逐题 run_single --engine graph
  (`runs/spec-baseline-graph-20261008`,35/35 退出码 0,report.json 复核 verdict 全 resolved、
  bug_id 去重 35)。与历史 fake35-v2/graph35-v2 的 35/35 同分布——回放基线没有漂移。
- **ADR-0009**(docs/adr/0009-严格验收契约-资源账本-恢复证据.md):把 spec §3.1–3.3 写成
  契约——严格 resolved 保留 + validation/resource/gate 三个解释维度;ResourceLedger
  (call_id 入账、provider/estimated/unknown 来源、超限结构化收尾、pending 崩缺记 unknown 不写 0);
  task_timeout 覆盖"执行线程开始→最终验收"、恢复不重授;成功绑定
  task_spec/source/baseline/candidate/test_policy 哈希与 verification_attempt_id;
  round_no 单点递增;指标语义变更必须带版本。锚点节按测试格式钉在 7 处**缺陷现场**
  (README:8 / nodes.py:861 finish / resume.py:43 / pytest_adapter.py:189 /
  metrics.py:125 / config.py:74,93),后续每卡修复到哪锚点同步修订到哪。
- 首个 commit 仅文档(spec 副本、ADR-0009、基线清单、TODO/PROGRESS、spec §10 索引)。

## S01 测试身份完整匹配(F3 关闭)(2026-10-08,产品卡 1/13)

缺陷(review F3,P0):`_matches_requested` 在 junit 带 file 属性时只对 file+方法名,
请求 `tests/test_x.py::TestA::test_same` 时 `TestB::test_same` 通过也算 all_passed;
同时 `validate_test_ids` 拒绝合法参数化 id `test_tuple[(1,2)]`(括号/逗号不在旧字符域),
executor 白名单放行了、入口预检却拦着,整条接入链是断的。

**修法**:新增共享解析器 `app/adapters/test_identity.py`——node id 解析成结构化身份
(POSIX 文件 + 完整类链 + test* 函数 + 参数化段),JUnit testcase 三元组逐段核对:

- @name 必须与 `function[params]` 逐字一致;参数段允许 `()`/逗号/引号/空格/等号等数据字符,
  拒 shell 元字符与控制字符(argv 单元素下只有注入语义);
- file 匹配按路径段边界(`othertests/` 顶替不了 `tests/`),**且 classname 必须以
  "点分模块路径+完整类链"结尾**——类链缺段、同名不同类、模块函数↔类方法互相顶替一律拒绝;
- 无 file 属性保留 rootdir 后缀兼容,同样要求完整类链;
- `all_passed` 升级:每条请求 id 必须恰好对应**一个**去重 testcase(0=缺失/deselected,
  ≥2=身份歧义)且其 status=passed;rc/timeout/零失败/零错误/零跳过检查原样保留。

**预检语义(spec S01 授权)**:第一版只验收具体函数/方法 node id——文件/目录/仅类
selector 在 `validate_test_ids` 明确拒绝(实测题面 35 题 147 条 id 全部是具体形态,
SWE 题 230 条具体形态,零兼容面损失);路径逃逸检查放在结构检查**之前**,
保留 `escapes the workspace/workspace-relative` 的既有消息分类(test_bugset 既有断言不动)。

**复现与回归**:
- 单元矩阵 `tests/test_test_identity.py`(22 例):同名类不互认、模块↔类方法、嵌套类链、
  参数化精确、file 缺失、rootdir 变化、Win 分隔、路径边界、`-p`/`../`/盘符/UNC/换行/NUL 拒绝;
- 真实 pytest 仓库(`tests/test_executor.py` 新 6 例):F3 由假通过变拒绝(TestA 红时
  TestB 绿不算过)、嵌套类、`test_tuple[(1,2)]` 真执行按参数判定、缺失类拒绝;
- API 全链路(`tests/test_custom_task.py` 新 1 例):POST custom 任务带 tuple 参数化 ID,
  基线红→回放补丁→verify 只跑请求的那条参数 → FINISHED/resolved。
- 既有 3 处夹具写实化(非期望放松):junit 夹具 classname 从占位 `t` 改为真实点分形态;
  custom id 推导测试的 `::t1` 函数名改 `test_*`(N-22 散列语义不变);
  参数化夹具补 `ids=['(1,2)','(3,4)']`——**tuple 参数不带显式 ids 时 pytest 生成 v0/v1**,
  `[(1,2)]` 形态本就不存在,这是夹具错误不是产品语义。

证据:受影响 4 文件 163 passed;全量离线 pytest **759 passed + 3 环境 skip**
(唯一失败是 ADR-0009 锚点行号被本卡代码位移——锚点机制按设计要求同步,已修至
pytest_adapter.py:213 并复跑 docs_anchors 6 passed);ruff/format 绿(196 files)。
分支 commit 见 git log(fix(adapters))。

## S02a 资源账本与共享终局验收(F1 关闭)(2026-10-09,产品卡 2/13)

缺陷(review F1,P1):两引擎都只在发请求前查预算;最后一条回复(含 finish)的真实
usage 入账后循环直接返回,graph 的 finish 无条件写 resolved、plain 的判定段只看
两个布尔——README 契约"未超预算才 resolved"在终局没有兑现。审查探针实测:
预算 20000、末回复 50000,plain 50148 / graph 50168 双双 FINISHED/resolved。

**修法(ADR-0009 §1/§2 落地)**:
- 新增 `app/graph/resources.py` `ResourceLedger`:任务级线程安全账本——每次模型调用
  持久 call_id(请求前 pending,回复**先入账**再处理工具/finish;同 call_id 重复入账无效);
  usage 标 provider/estimated/unknown;回复推过限额 → 记录实际 overrun(Overrun 带
  call/stage/used/limit)、状态 exceeded;请求前总额检查(含单次输出预留)不够发 =
  exhausted(停止新调用,实际未超);异常/取消留下 pending 且无 usage → unknown,
  已用不写 0、余量不重授;输出上限 0 时记 `output_reserve_unavailable`(能力边界,
  不宣称绝不超账单)。
- `plain_loop` 接线:请求前 `ensure_request_fits`(抛错时带全 loop-local 用量,
  N-11 口径不蒸发)+ `begin_call`;`model.complete` 异常路径 `abandon_call`;
  回复先 `record_usage`——返回 overrun 时记 `resource_overrun` 轨迹事件并抛
  BudgetError,**本回复的任何工具(含 finish)不执行**。
- 新增 `app/graph/acceptance.py` `final_acceptance`:graph finish 与 plain 判定段
  **同一条**终局验收代码——非空 diff ∧ 完整测试身份 ∧ 门禁 ∧ 双跑一致(开启时)
  ∧ 资源 within_budget ∧ 未取消 ⇒ resolved;`double_run_mismatch` 从 nodes 迁入共享
  (plain 引擎补上此前没有的 verify_double_run 复核,比较臂不再少跑);
  `acceptance_from_state` 另做恢复防绕过:state 累计量与账本取大复核任务总额。
- graph:TaskNodes 持有账本(测试替身 settings 走 getattr 兜底),四个 run_plain_loop
  调用点(LOCALIZE/PLAN/PROPOSE/分支候选)同账本入账;finish 节点重写——resolved
  不再无条件,资源类拒绝 BUDGET_EXCEEDED、非资源类 VERIFY_FAILED,旧字段保留;
  runner 在 state 未携带三态时回读账本真相(预算提前收尾不走 finish 的路径)。
- plain driver:建账本传循环;判定段走 final_acceptance;异常路径带账本状态。
- 报告:`report_schema_version=2`,TaskResult 增 `validation_status/gate_status/
  resource_status`(默认 not_run/unknown,不冒充已验证);Markdown 渲染新增
  "状态与验收证据"节,旧报告缺字段如实显示。

**回归**:tests/test_resources.py 13 例(call_id 幂等/边界=limit/overrun/exhausted/
unknown 不清零/阶段与候选并行入账);tests/test_acceptance.py 12 例(全绿矩阵/验证过
但超额不 resolved/unknown 不 resolved/空 diff/取消/门禁/双跑不一致/无账本=unknown/
两引擎同事实同结论/恢复防绕过);tests/test_f1_budget.py 6 例(审查探针同构:graph 与
plain 末回复 50000→BUDGET_EXCEEDED 且用量入账;多工具超额回复工具不执行;超额 finish
不兑现;纯文本超额;中途异常用量保留+unknown)。

**预算异常是"可知消耗"不是 unknown**(全量闸抓到的语义错误,当卡修正):测试桩用
`BudgetError(tokens_spent=…)` 表达"这次请求花了多少"是合法控制流(N-11 口径)——
plain_loop 对 model.complete 抛出的 BudgetError 走 `record_usage(source=estimated,
total=tokens_spent)` 入账保留,只有**拿不到任何用量**的异常/取消才 abandon 为 unknown。
record_usage 相应增加 total_tokens/source 参数(总额入账供无明细的异常路径)。

锚点同步(机制按设计咬人):本卡位移了 nodes/plain_loop/runner 的行,ADR-0004:300、
ADR-0006:195、ADR-0007:453/138、ADR-0009:856 与 test_docs_anchors 的"七项门禁"钉点
(661→657,位移史补 →657 一步)全部随代码修订,锚点扫描 0 失配。

兼容性修复(非放行):测试替身 settings(SimpleNamespace)缺新键 → nodes.__post_init__
用 getattr 兜底(与本文件既有惯例一致);bug.id 同。

## S02b 持久化时间预算与执行边界(2026-10-09,产品卡 3/13)

缺陷(spec S02 时间口径,ADR-0009 §3):deadline 依赖进程内 monotonic 起点——
跨进程无意义,恢复时 runner 以"当前时刻"重建 started_monotonic,**把 900s
整段重授**;plain 验证段完全没有时间守卫;verify 双跑的最后一次 rerun 之后
没有复查。排队时间、恢复停机间隔全部不计,时间预算形同虚设。

**修法**:时间口径整体切换到**持久化的墙钟截止时刻** `deadline_epoch`:
- runner/driver 在**执行线程启动**时建立 `time.time()+task_timeout_seconds`
  (排队不计),写进 state 随 superstep 进 checkpoint;0=不限时的既有约定保留;
- `gates.ensure_budget`/`plain_loop`/`TaskNodes._deadline_overrun` 全部改为比对
  `deadline_epoch`(参数与字段从 started_monotonic+time_budget_seconds 更名,
  测试替身调用点同步更新);duration 计量仍用 monotonic,两种时钟不再混装;
- **恢复不重授**:`prepare_resume` 从 checkpoint values 读回原 deadline 原样带上
  (停机间隔自然计入);没有可信 deadline 的旧检查点 → 拒绝恢复,且 runner 把
  "拒绝恢复"与"回退冷启动"分开——`TaskNodes.resume_rejected_reason` 置位时收敛
  NEEDS_REVIEW 而不是冷启动(冷启动会把时间/循环额度整份重发,S03 将泛化为
  结构化 ResumeDecision);
- **执行边界**:graph verify 四处复查(入口/failed 集后/双跑前/**最后一次 rerun 后**,
  最后这处是本卡新增)与 plain 驱动器的六处复查(baseline 前、verify 前/后×2、
  双跑前/后)全部就位——越时即 `BUDGET_EXCEEDED` 结构化收尾,已拿到的部分证据
  (verify 布尔)随状态保留,`resource_status=exhausted`,不得 resolved;
  plain 用内部哨兵 `_DeadlineReached` 带证据跳进 finally,绝不落进 NEEDS_REVIEW。

**回归**(tests/test_time_budget.py 4 例):过去时刻的合法检查点 → 恢复即判超时
(轨迹证明走的是恢复路径而非冷启动);无 deadline 的旧检查点 → NEEDS_REVIEW +
resume_unavailable 事件;最后一次 rerun 后越时 → 证据保留 + exhausted + 路由 end;
plain 驱动器 deadline 已过 → verify 一次 pytest 都不发起。
既有测试更新:test_graph 三个时间用例改墙钟口径(时钟注入从 monotonic 换 time),
test_plain_loop 更名 without_deadline,test_resume 的 stub state 补 deadline_epoch
(新格式合法检查点的夹具义务)。

全量离线 pytest(闸记录见提交);ruff/format 绿。

## S05a 规范任务契约 TaskSpec(F4 前半)(2026-10-09,产品卡 4/13)

缺陷(review F4,P1):custom 任务 ID 把 failed/regression/allowed_paths 元素**裸拼接**
后散列——failed=[a],regression=[b,c] 与 failed=[a,b],regression=[c] 得到同一个 ID,
基线/验收契约不同的两个任务可能命中同一个在途幂等键;持久化只存截 500 字符的 issue,
测试清单/回放脚本不落盘,任务不可忠实重建。

**修法**:
- 新增 `app/task_spec.py`(schema_version=1,身份唯一权威,两卡共用同一套哈希算法):
  具名字段分组(输入/源码/执行/环境/回放/溯源);canonical 形态 =
  `json.dumps(sort_keys=True, separators=(",",":"))` 取 SHA256——**字段边界由 JSON
  结构表达,永不分隔符拼接**;`created_at` 与哈希本身不入内容身份;测试列表按实际
  执行顺序参与身份;allowed_paths 空列表按既有 None 语义规范化;
  `effective_policy` 整份冻结(token/timeout/context/plan/branching 全键具名),
  恢复不得重读 .env 换策略;prompts_hash 钉住提示词模板原文;
  `from_json_dict` 校验 schema 版本与哈希自洽,篡改/旧版本一律拒绝。
- 源码身份:`fingerprint_source_dir` = 排序 (相对路径, 类型, sha256(字节)) 清单的总哈希
  ——实际文件字节,不随 mtime,不跟随符号链接,排除 .git,不可读留痕不静默跳过;
  `source_commit` 无 .git 返回 None 不编造。S06 冻结快照落地前如实只称"目录内容指纹"。
- graph runner 接线:同一 task_id 再次执行(含恢复)必须对照**受理时落盘**的
  run_dir/task_spec.json 重算指纹——不一致 = INVALID_TASK/source_changed 且模型零调用、
  原契约不被改写;一致则复用原契约(身份不随重跑漂移)。tmp+os.replace 原子写。
- 存储:tasks 表增量三列(task_spec_json/hash/schema_version),PRAGMA 现状补列——
  迁移可重复、事务化、旧行 NULL 可查询;Repository.set_task_spec 仅非终态可写,
  get_task_spec 对未落契约的旧行返回 None(调用方按"不可自动恢复"处理)。

**回归**:tests/test_task_spec.py 16 例(F4 的两种分组得到不同哈希/同输入稳定/
顺序与 issue 与策略与模型与 replay 各自改身份/issue>500 忠实重建/NULL 规范化/
roundtrip/篡改与旧版本拒绝/指纹六态/落盘规范形态/runner 契约落盘/source_changed
拒绝且模型零调用且现场保全);tests/test_storage.py +2(契约列回读、旧库迁移幂等
且保行)。ADR 锚点随 runner 行号位移同步(:188)。

全量离线 pytest(闸记录见提交);ruff/format 绿。S05b(S03/S04 后)接 API 幂等与
完整持久化端到端,不重复定义哈希。

## S03 候选工件与恢复再验证(F2 关闭)(2026-10-09,产品卡 5/13)

缺陷(review F2,P0):恢复把 propose/apply/verify/rollback/finish 之后的工作区一律
复位到基线,再直接执行检查点的 next 节点。next=finish 时补丁已被复位清掉,finish 仍按
检查点里的旧 verify 布尔判 resolved——磁盘 diff 为空、报告宣称成功(审查探针实测复现)。

**修法(ADR-0009 §4 落地)**:
- 新增 `app/graph/candidate.py`:apply 节点入口(= PROPOSE→APPLY 交接点,含分支合流)
  把**实际工作区 diff** 冻结为 `candidates/<id>/diff.patch + manifest.json`
  (tmp+replace 原子写;candidate_id 由 diff 内容派生,同 diff 幂等);manifest 带
  task_spec_hash/source_snapshot_hash/baseline_commit/round/父候选/生成 call_ids;
  读侧校验"清单可解析 + 补丁字节 sha256 与清单一致",半写/篡改一律不可信;
  state 只存 candidate_id/candidate_hash 引用。
- `prepare_resume` 重写为**结构化 ResumeDecision**(continue/revalidate/reject/
  already_terminal),runner 不再把恢复失败默认为冷启动(冷启动会把时间/循环/token
  额度整份重发):按 S03 规格表分流——localize/plan/propose/rollback=continue
  (propose 先复位、rollback **不再提前复位**:回滚节点要先保全 preserved_diff 再自己
  复位,旧实现提前复位会把取证毁成空 diff);apply/verify/finish=revalidate:
  候选存在性与 sha 校验 → 受理契约哈希/源指纹/基线逐一核对 → 余额检查 →
  复位前干跑落点校验(拒绝不毁现场)→ reset+重应用 → update_state 清旧 verify/gate
  结论并以 as_node="propose" 落检查点 → 从 APPLY 完整重验;END=already_terminal:
  只核对既有 report.json,不重跑、不再调用模型、不覆盖原终态。
- verify 每次真实重跑刷新 `verification_attempt_id`;TaskResult/report 增
  candidate_id/candidate_hash/verification_attempt_id——成功绑定**当前候选**与
  **当前验证尝试**。
- 服务 `_resumable_row` 纳入合法 checkpoint+candidate 边界:受理契约(task_spec.json)
  在场 + (有候选工件 ∨ 循环快照可用)才重新入队;旧任务缺身份证据不自动放行。
- 旧检查点无候选(next=finish 但 apply 入口从未冻结过)= F2 的真实历史现场:
  恢复按规格 **reject → NEEDS_REVIEW**,绝不沿用旧 True。

**回归**(tests/test_candidate_recovery.py 8 例):F2 种子(next=finish,旧 True+盘上有
补丁)无候选→拒绝且现场保全、有候选→重验证 resolved 且磁盘 diff==candidate_hash==
报告绑定;**verify→finish 边界 os._exit 真进程死亡**(子进程 monkeypatch finish 即死,
父进程按候选恢复到 resolved,轨迹含重应用/门禁/双跑复核);候选篡改/缺失拒绝;
END 恢复零模型调用零新事件;取消优先不 resolved;next=verify 的 revalidate 清旧旗标。
既有测试:prepare_resume 用例改 ResumeDecision 形态;僵尸夹具补受理契约;
恢复事件顺序维持"决策先于动作"。

全量离线 pytest(闸记录见提交);ruff/format 绿;ADR-0009 锚点随 S03 重构同步
(resume.py 缺陷现场锚点改钉修复位 REVALIDATE_NODES:67)。

## S04 统一轮次递增与拒绝后重试(F5 关闭)(2026-10-09,产品卡 6/13)

缺陷(review F5,P1):apply 节点把 round_no 从 1 预增到 2,route_apply 再拿**更新后**
的值比较 `< max_rounds`——max_rounds=2 的首轮门禁拒绝被直接判耗尽,第二轮永远不发生。
门禁拒绝与验证失败两条失败路径的轮次语义不一致(前者 apply 内预增、后者 rollback 递增)。

**修法(spec S04:单点递增+统一单线失败路径)**:
- apply 节点拒绝分支**不再返回 round_no**;route_apply 的门禁拒绝下一跳统一是
  rollback(builder 边表从 {verify, retry→plan, exhausted→rollback} 收敛为
  {verify, rollback});
- rollback 成为唯一递增点:保全 preserved_diff → reset(已拒绝候选复位,**不泄漏
  到下一轮**)→ round_no < max_rounds 则 +1 回 plan(M5 的"先重规划"语义由
  rollback→plan 既有边保持)→ 耗尽则 BUDGET_EXCEEDED("rounds exhausted after
  failed candidate",消息从 verify 专属改为候选语义);
- 副作用修正(向文档语义收敛):门禁拒绝从此也会经过 `_should_branch`——
  nodes.py 的分支触发注释本来就声明"verify 失败与门禁拒绝共用 repeat_streak",
  旧流程下门禁拒绝根本到不了分支判定;FakeLLM 回放(无 branch factory)不受影响。
- 阶段内 max_turns 含义不动;分支合流的递增路径( APPLY_PATCH→apply )不变。

**回归**(tests/test_round_transitions.py 8 例):F5 精确复现——max_rounds=2 首轮
门禁拒绝后第二轮必须真的发生(resolved 且 rounds==2、apply_gate×2、坏补丁不泄漏);
两轮全拒=恰好两次尝试+每轮 rollback 保全+第三次不执行;max_rounds=1 单次;
max_rounds=3 末轮翻盘;验证失败路径同样单点递增并恢复;首轮成功不进 rollback;
节点级:route_apply 拒绝统一交 rollback、apply 拒绝返回值不再含 round_no。
既有 test_graph 的拒绝重试/耗尽用例原样通过(语义收敛的旁证)。

全量离线 pytest(闸记录见提交);ruff/format 绿。

## S05b API 幂等/完整持久化/恢复接线(F4 收口)(2026-10-09,产品卡 7/13)

S05a 建好了契约模型,本卡把它接到业务两端:受理即双写,执行先核账,恢复按契约重建。

**修法**:
- **幂等键 = 受理时冻结的 TaskSpec 完整哈希**(service.create_task 构建 spec 后取
  `task_spec_hash`),取代裸拼接的 `bug_id|engine|model`——执行参数(max_rounds)与
  fake 回放脚本从此进身份(F4 的字段边界折叠与"不同 replay 共键"一起关闭);
  同输入同源码同策略的在途重提返回原任务,终态后重提生成新 task_id(既有语义不变)。
- **契约双写**:受理路径先原子写 run_dir/task_spec.json(执行与恢复读这份),
  再落 tasks 表三列;两处哈希同源。
- **执行一致性闸**:_execute_inner 开跑前核对 DB 与文件的契约哈希——不一致
  (半写/被改)= INVALID_TASK "task_spec inconsistent",模型构建器一次不调。
- **恢复按契约重建**(_bug_from_row):tasks 表带契约 → manifest 题回读题面并
  核对身份一致(漂移即拒);custom 任务从契约忠实重建(issue 完整原文、测试清单、
  scope、max_rounds,源指纹重算不一致即拒)——"custom 无从重建一律判死"的旧边界
  在有契约后解除;旧行无契约保持旧路径(正式题回读 manifest,custom 判死)。
  回放脚本经 spec.replay 传递,_execute 的 in-memory 脚本链路不变。
- 不变量保持:api_key/Bearer/代理凭据不进契约(有效策略只拷具名键);取消原子守卫、
  终态守卫、失败回滚(_rollback_created)原样。

**回归**:tests/test_custom_task.py +3(F4 的两种测试分组不再 coalesce/不同 replay
不 coalesce/issue>500 忠实重建——DB 列截 500 而契约与重建不截);
tests/test_service_robustness.py +2(custom BugTask 从持久化契约重建+源变拒绝/
DB 与文件契约哈希分叉时执行判 INVALID 且模型零调用)。既有幂等/取消/恢复用例全绿。

全量离线 pytest(闸记录见提交);ruff/format 绿。F4 全部关闭。

## S06 冻结执行输入与环境预检(2026-10-09,产品卡 8/13)

受理之后、执行之前的窗口里用户可以改源仓库——旧行为执行的是"执行那一刻"的内容,
与受理契约脱节;环境坏(依赖缺失/收集错误)要到引擎基线才暴露,还可能留下假 RUNNING。

**修法**:
- 新增 `app/gitops/input_snapshot.py` `freeze_input`:custom 任务受理时把源码复制进
  `run_dir/source_snapshot`(tmp+rename 原子就位);复制范围与 materialize_repo 的
  `_TEMPLATE_IGNORE` 同一集合(规则版本入库);**指纹对副本字节计算**——
  "哈希实际复制进去的文件";符号链接按链接复制后统一拒绝(绝不跟随读工作区外);
  源/快照互相包含拒绝;体量与耗时上限走新 Settings 键
  intake_max_files/intake_timeout_seconds(进 provenance EXEMPT 三分类);
  中途失败清理 tmp,源目录零写入。
- 新增 `app/api/preflight.py`:受理预检(解释器存在 → pytest 可用 → 两个测试集
  `--collect-only` 收集),**收集走受控执行流程**(executor run_tests),不在 HTTP
  handler 裸 subprocess;分类边界钉死——收集错误/依赖缺失是**环境问题**(消息点名),
  与业务断言失败分开;rc=4 按输出细分(带 ERROR 块=依赖缺失,否则=目标不存在);
  容器环境只验 daemon 可用(宿主机收集结论不可信);预检失败 → 422,不建任务行、
  不调模型、不留假 RUNNING。**收集在一次性临时副本上执行**——pytest import conftest
  会写 `__pycache__`,直接在冻结副本上跑会污染其内容指纹(实测踩中并修复)。
- service 受理接线:custom 任务 create_task 内完成 冻结→指纹核对(窗口内被改即拒,
  "nothing was accepted")→ 预检 → bug.repo_dir 指向冻结副本——**执行与恢复不再读
  用户可变源目录**;TaskSpec 增 `source_snapshot_ref`(常量相对引用,幂等键不受
  时间戳影响);intake 耗时如实入日志;快照随可弃集回收(指纹已在取证集)。
- 恢复重建(`_bug_from_custom_spec`)指向冻结副本并重算指纹,不一致即拒。
- manifest 题(平台自有目录)不冻结,指纹复核照旧;本轮不做通用环境构建器,
  API 请求不能注入 shell/Docker host/安装脚本(环境只来自平台配置与题目 manifest)。

**回归**:tests/test_input_snapshot.py 8 例(同字节同指纹/.git 与缓存剥离/未跟踪与
删除改指纹/符号链接拒绝且不留残档/包含拒绝/体量与超时上限/源目录 mtime 级零写入/
窗口内变更进不了已接受的快照);tests/test_preflight.py 6 例(成功面/解释器缺失短路/
依赖缺失=collection error 分类/rc=4 目标问题分类/断言失败不归预检管);
tests/test_custom_task.py +1 e2e(**受理后改源仍执行冻结副本**:workspace 内容=
冻结内容+修复、不含受理后变更、用户源目录零写回;本用例按 spec 单独关闭回收)。
既有 39 个 API/service 用例全绿。

全量离线 pytest(闸记录见提交);ruff/format 绿。

## S07 运行中进度与轨迹可查(2026-10-09,产品卡 9/13)

缺陷:轨迹 JSONL 只在任务**结束**时才整批入库——运行中查任务只有 QUEUED/RUNNING
两个状态,用户不知道"现在在哪一步、为何失败";实时进度无从谈起。

**修法(单进程最小方案,不引入 SSE/MQ/WebSocket)**:
- **Tracker 增可选 sink**:每条事件在 JSONL 落盘之后(JSONL 是原始取证来源与真相层)、
  **Tracker 锁之外**调用(service 把事件幂等写入 SQLite)——轮询方不持 Tracker 锁做
  数据库操作,锁顺序倒置无从发生;sink 抛错只计数(sink_failures)不打断运行,
  收尾 `_persist_artifacts` 的补录(INSERT OR IGNORE,返回**实际新插入数**)补齐全部
  缺口并打 WARNING——降级可观测,不谎称实时入库成功。
- **幂等去重**:(task_id, event_id) 唯一索引(SQLite UNIQUE 不约束 NULL,旧行照存);
  迁移纪律=先存后删——历史重复行整体复制到 `trajectory_events_duplicates` 留档再从
  主表去重,不为建索引静默删历史;迁移可重复、事务化。
- **进度列**:tasks 增 `stage`/`last_event_at`,由 sink 在入账事件时推进,**守卫到
  非终态行**——终态后迟到事件只进取证轨迹,生命周期与进度列都不被复活
  (取消×自然完成的原子守卫照旧)。stage 与生命周期 status 是两列:LOCALIZE 永远
  不会被误读成终态。
- **API**:TaskOut 增 stage/last_event_at(旧行缺列=None);offset/limit 轮询面不变。

**回归**(tests/test_live_progress.py 5 例):Event barrier 在 FakeLLM 内精确暂停——
运行中查任务可见 RUNNING+LOCALIZE 进度列、轨迹表已有事件,释放后到 FINISHED;
实时+补录并集恰好等于 JSONL 行数且 event_id 唯一;sink 全程失败仍 FINISHED 且
补录补齐;终态后迟到事件不改 status/进度列(事件本身入轨迹);TaskOut 进度字段在场。
既有 39 个 API/service 用例全绿。

全量离线 pytest(闸记录见提交);ruff/format 绿。

## S08 演示修复与 API golden path(F7 关闭)(2026-10-09,产品卡 10/13)

缺陷(review F7,P1):当前面试演示脚本提交旧字段 diff_text,apply_patch 单入口
只认 patch_text+块协议——真实离线运行 VERIFY_FAILED,轨迹明确记
"missing a required argument: 'patch_text'"。

**修法**:
- `demo/run_dirty_ticket.py` 重写:回放经 `unified_to_block` 生成块协议补丁
  (锚点取实际源文件,不借改 fixture);新增 `--out`;输出任务 ID/基线/变更文件/
  门禁/双测试集/validation+gate+resource 三态/报告与补丁路径;非 resolved 非零退出;
  **脚本自证演示目录零写入**(前后目录哈希比对,不一致退出 2)。
- 新增 `demo/run_api_ticket.py` API golden path(TestClient 进程内,离线):
  POST custom fake → 同键重提幂等(200+同 task_id)→ 轮询终态 resolved →
  轨迹含 apply_patch/run_tests/finish → 报告与资源状态 → diff.patch+冻结候选可读 →
  **独立任务取消(CANCELLED)** → **门禁拒绝(PATCH_REJECTED,plain 引擎——
  graph 的拒绝是过渡态会走轮次耗尽,语义不同)**;任一步失败非零退出。
- 新增 `demo/README.md`:两个脚本的运行命令 + 本地 uvicorn HTTP 人工演示路径
  (端口/鉴权/curl 命令;文档入口,自动化边界只有下面两个测试文件,不互相替代)。
- 回归:tests/test_demo_smoke.py(subprocess 真跑当前脚本:resolved、源目录哈希
  不变、轨迹 apply_patch 事件用 patch_text 且 diff_text 零出现、坏 --out 非零退出)
  + tests/test_api_golden_path.py(三个演示段经 TestClient 全链路 + barrier 驱动的
  取消确定性)。演示仓库测试期望零改动。

全量离线 pytest(闸记录见提交);ruff/format 绿。
