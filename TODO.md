# TODO.md — Agent 架构升级任务清单(2026-10-07 夜间 GOAL 循环)

最高目标:把 PatchPilot 的仓库级缺陷修复 Agent 推到行业前沿水平(参照 deepseek-harness / pi agent),
攻坚四个领域:上下文管理、工具链与 ACI、行动范式、状态持久化与中断恢复。

## 约束(每个子任务都必须遵守)

- 不削弱任何门禁/预算/白名单语义;新机制一律**附加**而非**放行**(AGENTS.md 禁区)。
- 单次生成 ≤ 300 行;每个原子任务 = 代码 + 用例 + `ruff check . && ruff format --check .` + `pytest -q` 全绿 + 一个 commit。
- 零成本验证:真实 LLM 批**不自行发起**(需用户授权)。回归证据用 fake 引擎回放 graph 35 题,
  对照基线 `runs/graph35-v2`(逐题 verdict/rounds/changed_files/gate_violations 等 9 字段)。
- 允许动 `app/graph/plain_loop.py`、`app/tools/`、`app/config.py`、`app/api/service.py`;
  动 `app/graph/nodes.py`/`gates.py`、`app/gitops/`、`app/evals/metrics.py` 的判定语义时,
  只做**附加**并把变更写进 ADR,不得改门禁规则本身或测试期望。

## 现状诊断(证据见 PROGRESS.md 的 D 段)

| 领域 | 已有 | 缺口 |
|---|---|---|
| 上下文 | 单条消息列表构建器、逐结果 fold、按段预算份额、findings 文本通道 | 无滑动窗口/无历史裁剪(超预算只能终止)、无语义压缩、无持久记忆(仓库骨架从不注入) |
| 工具/ACI | 块协议局部编辑(锚点唯一性校验)、超时 + 进程树 kill + 输出截断、结构化测试报告 | 检索是 Python 子串匹配(非 ripgrep、无上下文行/排序/单文件上限)、零 AST/符号能力、read_file 无 limit |
| 行动范式 | LOCALIZE→PROPOSE→APPLY→VERIFY + rollback + 自适应 Best-of-N | 无独立 Plan 阶段(计划只是自由文本),反思只给文字提示且 rollback 后模型丢失"我试过什么" |
| 容错 | SqliteSaver 逐 superstep 落盘、trajectory.jsonl | checkpointer **纯留档**:全仓无 get_state/resume/replay,崩溃后 `recover_stale` 一律判 NEEDS_REVIEW,不续跑 |

## 里程碑与原子任务

### M1 层级化上下文 + 滑动窗口压缩(最高优先:直接打掉"LOCALIZE 只读调查花光 400k"这一实测死因)

- [x] M1.1 `app/context/token_window.py`:纯函数 `compact_messages(messages, ...)`;固定不可压缩区
      (system + 首条 user + 最近 N 轮),更早的 tool 结果替换为单行存根(工具名 + 关键入参 + 原尺寸),
      仍超量则整组丢弃最旧的 assistant/tool 对。确定性、零 LLM 调用。
- [x] M1.2 接线进 `run_plain_loop`:每次 `model.complete` 前按软阈值压缩;落 `context_compact` 轨迹事件
      (压缩前后 token 数、存根数、丢弃数)。`BudgetError` 触发条件一字不动(压缩后仍超 → 照旧终止)。
- [x] M1.3 `Settings`:`context_window_tokens`(软阈值,0 = 关闭 = 旧行为)、`context_keep_recent_turns`。
      两键进 provenance `SNAPSHOT_KEYS`(决定轨迹形状,跨批次可比)。
- [x] M1.4 用例:压缩纯函数边界 15 例 + 循环接线 6 例(含"关闭时消息序列逐字不变"的回归钉子)。
- [x] M1.5 给 `context_window_tokens` 定**生产默认值 = 16000**(推导写进 config 注释与 ADR-0004:
      折叠后的单条回执约 800-1000 tok、6 回合尾巴约 6-7k,16k = 超出在用尾巴约 9k 才让位);
      同时把 `run_plain_loop` 的两个阈值参数改成 `None = 跟随 Settings`,
      避免"graph 臂压缩、消融臂不压缩"这种未登记消融变量。
      **未证明的部分如实挂着**:阈值对真实模型修复率的影响要真实批次标定(待用户授权)。

### M2 持久记忆:仓库骨架注入

- [x] M2.1 `app/context/repo_map.py`:stdlib `ast` 出 Python 符号大纲(class/function/async/装饰器 + 行区间 + 签名),
      加截断后的文件树;渲染成 `<repo_skeleton>` 文本块,受 `repo_map_max_chars` 约束。
- [x] M2.2 工作区边界复用现有校验(`SKIP_DIRS` 直接 import 自 `app.tools.files`、符号链接与非 UTF8 只进树不解析);
      单文件解析失败只降级该文件,外壳异常降级为"无骨架"。
- [x] M2.3 注入 LOCALIZE 与 PROPOSE 的持久记忆区(追加到 `extra_system`,与 system 同层,不参与压缩);
      **默认开启**(`repo_map_enabled=True`),关闭时系统提示逐字不变。
- [x] M2.4 用例 13 例:大纲行号正确性、嵌套一层、局部函数不出、签名重建、坏文件降级、
      非 Python/二进制只进树、`SKIP_DIRS`、截断可见且无半截行、空仓库、外壳异常降级、开关两态形状。

### M3 工具链与 ACI 升级

- [x] M3.1 `search_code` 重写:可用 ripgrep 则走外部二进制(带超时与二进制缺失回退),否则纯 Python 路径;
      增加上下文行、单文件上限、`regex` 开关;总结果仍受 `max_search_results`。
      **实现口径比原计划更严**:rg 只做"文件级预筛"(`--files-with-matches` + 显式文件参数),
      行号/文本/裁剪只有 Python 一条实现——差分探针证明 rg 的断行与 BOM 处理会让行号错一位。
- [x] M3.2 新工具 `find_symbol(name, kind?)`:AST 定义跳转,返回 `file:line:kind:signature`,上限保护。
- [x] M3.3 新工具 `describe_file(path)`:单文件 AST 大纲(符号 + 行区间),让模型按区间精读而非整文件灌。
- [x] M3.4 `read_file` 增加 `limit`(与 offset 组合成精确窗口,且被 `max_read_lines` 夹扣);越界与二进制拒绝口径不变。
- [x] M3.5 注册 schema + 阶段白名单(LOCALIZE/PROPOSE 可用),**消融臂 `ONE_SHOT_TOOLS` 同步**
      (两臂只差登记变量);每工具用例 + 轨迹留痕;等价性用例钉住 rg/Python 两路同输出。
- [x] M3.6 **检索遍历域与输出体量解耦**(证据见 PROGRESS.md D.12):`list_files` 保留 500 条**输出**
      上限并如实标 `truncated`;`search_code`/`find_symbol` 改走独立上限
      `Settings.max_search_files=8000`,没搜全时输出 `scope_truncated=True`。
- [x] M2.5 骨架在大仓库改出**目录级汇总**(`pkg/  (8 files, 8 py)`,深度由 `repo_map_dir_depth` 定),
      覆盖全部文件;小仓库形状逐字不变。

### M4 反思机制升级(廉价且高价值)

- [x] M4.1 失败轮把 rollback 前保真的 diff 摘要(文件 + 每文件增删行数 + 关键 hunk 头)拼进下一轮反馈,
      让模型知道"上一版改了什么、为什么没生效"——当前 rollback 后模型对工作区已无痕迹。
      实现:`app/graph/reflection.py::diff_digest` + `with_discarded_patch`,接线只在 rollback 返回体。
- [x] M4.2 反馈里带"必须改变的假设"要求(换文件/换分支条件/换前置修复),
      且**只给形状不给补丁正文**——正文会诱导模型逐字重放上一版。
- [x] M4.3 用例:diff 摘要形状与整行裁切上限、空 diff/非 diff 出空串、无回滚时反馈逐字不变、
      节点级真回滚行为、末轮耗尽仍终止且 `preserved_diff` 保全;`repeat_streak` 计数口径一字未动。

### M5 严格的 Locate→Plan→Act→Verify

- [x] M5.1 `TaskState` 增 `plan` 字段(可序列化,进 checkpoint)。
- [x] M5.2 `builder.py`/`nodes.py` 在 localize 与 propose 之间插 `plan` 节点:一次结构化调用产出可执行计划
      (目标文件/符号、改动意图、预期验证),计划文本被 pin 进 PROPOSE 上下文并进轨迹;VERIFY 失败时回流 PLAN 修订。
      复核补:plan 也注入仓库骨架(计划要能点名文件与符号)。
- [x] M5.3 `Settings.plan_stage_enabled`(默认开)与 `plan_budget_share=0.15`;关闭时零请求、
      `state.plan` 恒空、PROPOSE 渲染逐字节不变;plan 调用走既有预算/时间/取消检查,
      `recursion_limit` 由 `4N+8` 抬到 `5N+8`。
- [x] M5.4 用例:阶段顺序、失败轮回 PLAN 并带上轮反馈、降级仍进 PROPOSE 落 `plan_degraded`、
      任务级耗尽仍 `BUDGET_EXCEEDED`、脚本模型零额外步消耗(`consumed` 相等)、
      `TaskState.plan` 可 checkpoint、fake 图路径 25 例一行未动仍绿。
- [x] M5.5 文档对齐:已随 M7.3 落地并复查过 —— `docs/design.md:99` 与 `docs/adr/0001:27`
      都写 `5N+8`(并注明此前是 `4N+8`);历史审计文档按 M7.3 的口径保留原文间不改。

### M6 状态持久化与中断恢复(checkpointer 从"留档"变"可续跑")

- [x] M6.1 循环层 turn 边界快照:落 `run_dir/loop_state.json`(压缩后的消息列表 + 阶段/轮次/turn/已耗用量计数),尺寸上限保护。
- [x] M6.2 `runner` 增恢复入口:`get_state(thread_id)` 读检查点 + 循环快照 → 重建 ToolContext → 从中断阶段续跑;
      快照缺失/损坏/版本不符 → 退回"阶段重跑/判死",落取证事件。
- [x] M6.3 `service.recover_stale`:有可续跑快照的 graph 任务重新入队续跑(用既有任务锁防双恢复,不 force_release);
      不可续跑的保持现口径。**硬约束已落地并有用例**:恢复后 APPLY/VERIFY/门禁必须真重跑,不复用中断前的"已过闸"结论。
- [x] M6.4 用例 29 例:turn 边界 kill 后续跑且不重放已完成轮次;快照损坏/版本不符/尺寸超限三种降级;
      原子写半途崩溃;可写阶段续跑前工作区确被复位、只读阶段现场原样;拒绝续跑不得先毁现场;
      递归额度只顺延剩余(崩溃不换来更多循环);无快照僵尸仍判 NEEDS_REVIEW;双恢复被锁挡住。

### M7 证据、文档与收口

- [x] M7.1 零成本对照回放(2026-10-08 00:36 完成):graph 35 题 fake 重跑 vs `runs/graph35-v2`
      → **判定字段逐题 0 条差异**、35/35 resolved;唯一差异在成本档
      `turns 249→284`、`tokens 6917→7445(+7.6%)`,逐题 `+1 turn` 且 `tools/PLAN: llm 0→1`
      = M5 计划段那一次调用。`context_compact` 0 次(fake 语料只有 7-9 轮,够不到 16k 阈值)
      ⇒ 只证明"压缩不干扰既有判定",**不证明压缩省额度**;效果主张必须真实模型批。
      测量工具即本夜新写的 `scripts/compare_batches.py`(判定字段逐题不等即退出码 1;
      成本字段只报差异;`--tools` 出逐阶段工具调用直方图与 `context_compact` 事件数)。
- [x] M7.2 ADR:上下文分层与压缩策略(0004)、检索引擎(0005)、崩溃恢复两级与"闸必须重跑"(0006)、
      计划工件(0007)。每篇含反方与"未证明"条目;锚点节按 `tests/test_docs_anchors.py` 的严格形状
      (`- \`路径:行号\` — \`子串\``)逐条验真。
- [x] M7.3 `docs/design.md`/`docs/adr/0001`/`app/graph/checkpoint.py` 与 README 索引对齐实际实现
      (recursion_limit 5N+8、转移表含 plan、checkpointer 从"仅留档"改为"留档 + 恢复位置权威");
      历史审计文档不改(时间戳证据)。
- [x] M7.4 全量收绿:`ruff check .` + `ruff format --check .` + `pytest -q` →
      **687 passed, 3 skipped, exit 0**(开工基线 536,净增 151 例;第 3 条 skip 是
      Windows 无符号链接创建权限的定向用例);graph 35 题零成本回放对基线判定零回归(见 M7.1)。

### M8 接线级行为证据(收口后补的一卡)

- [x] M8.1 `tests/test_context_wiring.py` 4 例:压缩经图外真实循环触发并落 `context_compact` 事件
      (且循环仍能收束)、PLAN 那次请求的 system 里真有 `<repo_skeleton>` 与真实符号名、
      四个新开关同时关闭时判定与 `changed_files` 与引入前同形、`PLAN_MARKER` 由提示词与
      脚本模型共用同一常量。
      阈值仍在用例里显式调小:fake 语料每题 7-9 轮到不了生产默认 16000,
      **默认值省了多少额度不在零成本口径里主张**(ADR-0004 反方条目)。

### M9 两臂齐证 + 真进程死亡续跑(2026-10-08 01:10,M6/M7 证据的最后一块)

- [x] M9.1 plain 臂零成本回放对照:`runs/fake35-v2`(基线,锚 commit `d50f251`)vs
      `runs/night-plain-final-2026-10-08`(现主干,锚 `b6c6d8e`,worktree 干净)→ 35 题
      **判定字段逐题一致**,成本档 `turns 214→214`、`tokens 6225→6225` 完全相同,
      只有 `duration_ms` 逐题 ±0.1~1.7s 抖动。与 M7.1 的 graph 臂合起来才叫"两臂对齐":
      新机制没碰旧引擎的判定与消耗路径。
      如实记一条口径杂音:基线批的 provenance 写着 `llm_enabled=true`(那是当时的 Settings 值),
      但它的 `model_provider=fake-replay` 与每题 ~180 token 的量级证明实际用的就是 FakeLLM;
      两臂都零花费,对照成立。
- [x] M9.2 `tests/test_resume_crash.py`:崩溃方跑在**子进程**里并以 `os._exit(7)` 死亡
      (不走 `finally`、不关 SqliteSaver 连接、不删快照),续跑在测试进程发起 ——
      这是"真实进程被杀"而不是"抛异常被收敛",M6 之前只证过后者。
      断言钉死:检查点能被新进程重开、A 级快照停在工作记忆第 1 轮、脏补丁落盘、
      续跑前 `rolled_back=True` 复位、门禁/双测试集在续跑这次真重跑、已完成阶段不重放。
- [x] M9.3 文档盲区修补:README 的能力清单("7 个受控 Agent 工具"、状态链缺 PLAN)按 M3/M5
      的实际实现回改,并在 `test_docs_anchors.py` 加一条以 `app/tools/registry.py` 为准绳的
      逐名对齐用例 —— file:line 锚点盯不住"条数与名单"这类漂移,得让代码当准绳。
- [x] M9.4 生产默认阈值"会咬人"的用例(`test_token_window.py` 第 16 例):按真实 PROPOSE 轨迹形状
      (每条 tool 回执 ≈ 窗口 1/8、20 组)压回 Settings 的 `context_window_tokens`,
      同时钉住"pinned 头原文不动 + 最近 6 轮原文可见 + 无孤儿 tool 消息"三条请求合法性不变量。
      尺寸全部从 Settings 推导,不写死数字。**仍不主张省了多少额度**(fake 语料到不了这个尺寸)。
- [x] M9.5 README 数字全量对账(照 P3-7 的路子,拿代码/夹具当准绳逐条数):
      端点 7(`app/api/routes.py` 装饰器计数)= / 题目 35(`bugs/BUG-*` 目录)= / 攻击样例 10
      (`bugs/attacks/*` 目录)= / 隔离实验 5(`docs/docker-isolation-notes.md` 结果表行数)= /
      hard 段 BUG-029..035 存在 = 。**只有工具条数与状态链这两条是错的**(M3/M5 之后没回改),
      已修并由用例钉住;其余四条本轮实测核对通过,其中题数/攻击样例数也补成了用例
      (`test_readme_dataset_counts_match_the_fixtures`,拿 `bugs/` 目录当准绳 ——
      R3-Q1 那条"ATTACK-009 入库后「8 个」没回改"就是这类漂移,现在对着实物数)。

### M10 实测替换推算,并修掉一条假通过(2026-10-08 01:30)

- [x] M10.1 `tests/test_context_wiring.py` 压缩用例的夹具 `_big_repo` 原来只是普通临时目录,
      而 `run_plain_loop` 收尾要跑 `working_tree_diff` 判 `patch_applied` → 非 git 目录必抛
      `GitCmdError`。它此前能绿几乎可以确定是因为 basetemp 落在本仓库的 gitignored 目录内
      (假通过,与临时目录位置绑定)。改为 `materialize_repo` 的真 git 工作区,并补
      `outcome.patch_applied is False` 把那一步 git 判定纳入证据。
- [x] M10.2 `recursion_limit = 5N+8` 的斜率实测化:新用例读检查点 `metadata.step`,
      N=2/3/6 实测 13/18/33 ⇒ 每轮 5 步、固定段 3 步、余量恒 5 且与轮数无关;
      runner.py 里那段自称"前缀 4 步+每轮 5 步+收尾 1 步"(=5N+5,与实物不符)的推算注释
      改为实测口径,**只动注释、行为零改动**(禁区)。
- [x] M10.3 流程红线补记:套件在跑时**不得**另开 pytest —— 嵌套 pytest 用机器默认 basetemp 根,
      并发会让 `os.scandir` 撞 `PermissionError [WinError 5]`(本夜 `test_bugset[BUG-015]` 即此因,
      非主干缺陷)。**M11.4 更正:另开进程只是放大器** —— 第二轮没有任何并发时同一处仍红,
      根因是所有嵌套执行共用同一个机器默认临时根(加上套件自身有并发路径)。
- [x] M10.4 同类"位置相关"夹具的系统性排查:按**会碰 git 的入口**清点 —— `run_plain_loop` 在
      tests 里的 20 个调用点(test_cancellation / test_graph / test_localize_budget_share /
      test_plain_loop / test_context_wiring)传的 workspace 全部来自 `create_workspace` 或
      `materialize_repo`,只有 M8 那一条是裸临时目录(已修)。另确认仓库根没被这些用例写脏
      (`unused.jsonl` 之类不存在、`git status` 无残留 staged 项)。
- [x] M10.5 机制化防线:`tests/conftest.py` 加 session 级 autouse 断言 —— basetemp 落在仓库之内
      就**当场整体报错**(而不是让某些用例悄悄变绿)。CI 用默认 basetemp(系统临时目录),不受影响。

### M11 发现:被诊断仓库的 pytest 没有独立临时根(2026-10-08 01:44,只登记不擅改禁区)

- [x] M11.1 事实核对:`app/adapters/pytest_adapter.py:113` 造的命令是
      `python -m pytest -q --color=no -o junit_family=xunit1 [--junitxml=…] <ids>`,**没有**
      `--basetemp`,也没给子进程换 TMPDIR/TEMP。于是被诊断仓库自己的测试用的
      `tmp_path` 全部落在 `<系统临时目录>/pytest-of-<user>/pytest-N/` ——
      所有并发执行共用同一个根,而 pytest 只"保留最近 3 个编号目录"。
      **M11.5.0 更正:这条结论只核到了命令构造器,执行层是错的** ——
      `build_pytest_cmd` 确实不带 `--basetemp`,但它的调用方 `run_pytest` 在
      `app/adapters/pytest_adapter.py:255` 追加了 `--basetemp=<junit 所在目录>/basetemp`,
      所以本地后端的执行**不落在机器默认临时根**。我当时只读了 113 行就下结论,没顺着调用方走。
      M11.4 观察到的 `PermissionError` 因此另有归属(外层套件自身的 `tmp_path` 用机器默认根,
      与并发的嵌套 pytest 共用 `pytest-of-<user>`),`_isolated_nested_temp` 那条修法仍然成立。
- [x] M11.2 修法已裁决并落地(2026-10-08 12:40,`a127b28`,细节见 M11.5.5):
      ①的形态改成**每次执行私有**(不是按 run_dir/report_dir),②(执行器 env 重定向 TEMP)
      经核对**不需要**——平台所有测试执行都走 `run_pytest`,临时根已私有;
      被诊断仓库里裸 `tempfile.mkdtemp()` 落的是 `%TEMP%` 根本身,那一层实测可写
      (坏的只是 `pytest-of-<user>` 这一层,见 M11.6.2)。
- [x] M11.3 生产影响评估(需要它才能定优先级):同一平台上并发任务数 >1 时,
      一次长验证的 `tmp_path` 可能被更新三次执行后的清理**删掉**,那时得到的不是 flaky
      而是**假 VERIFY_FAILED**;本夜只在 Windows 上拿到 `PermissionError` 这一半证据,
      没做过"三个并发任务互删临时根"的正向复现。
      **M11.5 已把这条答完并闭合**(账目原本漏勾,2026-10-08 由 M15 对账时补正):
      正向复现拿到了 —— 共享 report_dir 的并发执行伪失败 **9/18**(隔离组 0/18,控制组全过),
      失败三副面孔 `FileNotFoundError`/`FileExistsError`/清理侧 `PermissionError`;
      裁决与落地见 M11.5.5(`a127b28`),回归钉子 `tests/test_basetemp_isolation.py` 带反向验证。
- [x] M11.4 更正 + 测试侧收口:第二轮全量(01:32→01:51)期间没有任何会起嵌套 pytest 的并发,
      `test_bugset[BUG-015]` 仍以同样的 `PermissionError` 红 —— 所以根因是**共用机器默认临时根**
      本身(套件内部就有并发路径),不是外层 `--basetemp` 挑错了值。
      `tests/conftest.py` 加会话级 `_isolated_nested_temp`:把 TEMP/TMP/TMPDIR 指向本次会话私有目录
      (执行器的 env 白名单本来透传这三个键,故无需动生产代码)。生产侧仍按 M11.2 等裁决。

### M11.5 生产影响评估:临时根在并发执行下到底会不会互删(2026-10-08 上午,零成本)

- [x] M11.5.0 更正 M11.1(见上)。据此重新确定"临时根"的实际归属:每次执行的 basetemp
      **由它的 junit 目录派生**(`(junit.parent / "basetemp")`)——
      主流程 `run_dir/reports/junit-*.xml` → `run_dir/reports/basetemp`;
      Best-of-N 候选 `run_dir/reports/cand{i}/branch-*.xml` → `…/cand{i}/basetemp`。
      于是风险从"所有执行共用机器根"换成一个**不同的**问题:**同一任务内共享 report_dir 的
      并发执行会共用同一个 basetemp**,而 pytest 在 `--basetemp` 给定时无条件先删后建
      (`.venv/Lib/site-packages/_pytest/tmpdir.py:154-159`:`if basetemp.exists(): rm_rf(basetemp)`)。
- [x] M11.5.a 今天的实际拓扑核对(读代码,不测):主流程 verify 的 4 次 pytest 是同线程串行
      (`nodes.py:799/806` 在 `verify` 节点内顺序调用),候选并发但 `cand_reports` 按 index 分目录
      (`nodes.py:907`),跨任务是不同 run_dir(`service.py` 线程池 + `run_dir` 含 task_id)
      ⇒ **当前没有"同 basetemp 并发"的真实路径**。所以 M11.2 的紧迫性不能靠推定,要靠测量。
- [x] M11.5.1 真机测量已跑完(2026-10-08 11:41→11:52,零成本、无模型;
      原始输出**已入库**:`docs/evidence/2026-10-08-basetemp-contention-prefix.txt`
      (修复前,共享落键)与同目录 `...-postfix.txt`(修复后重跑,两臂都 0/18);
      换机器接力时别再指 `D:\tmp\...`,那不在 git 里):
      变量只有一个——**report_dir 是否共享**;工作区每次都另拷贝一份(排除工作区复用这个混淆项)。
      ①控制组:单执行 1 次,确立"这题本来能过"(all_passed=True)基线;
      ②同 report_dir 并发 K=2、K=4;③不同 report_dir 并发 K=2、K=4;各 3 轮;
      ④机器默认根泄漏计数:`<temp>/pytest-of-*` 编号目录在执行前后的变化(验证更正后的说法);
      ⑤被诊断测试写成两种:快版(纯 IO)与慢版(每次写文件间 sleep)。**慢版是放大器,登记在此**:
      它只放大窗口,不改变机制;若快版已经出伪失败,结论就不依赖放大器。
      **判据跑之前写死**:
      (A) ②出现 ≥1 次"控制组能过而并发组 all_passed=False"⇒ 同 report_dir 共享 basetemp 是**真实缺陷**,
          M11.2 必须做,且方案①的落键粒度必须是**每次执行唯一**(不能按 run_dir/report_dir);
      (B) ②为 0 且③为 0 ⇒ 现有串行拓扑下无实际损害,把"basetemp 按执行唯一"降级为
          **不变量用例 + 注释**(防未来新增并发时踩坑),不做语义变更;
      (C) ④显示执行前后机器根有新增 ⇒ M11.5.0 的更正还不完备,回头继续查是哪条路径没走 `run_pytest`。

- [x] M11.5.2 实测数字(判据 (A) 成立,放大器不承重):

      | 组 | 同 report_dir 并发 | 隔离 report_dir 并发 |
      |---|---|---|
      | 快版(无 sleep,单跑 2.13s) | **伪失败 9/18**(K=2:3/6,K=4:6/12) | 0/18 |
      | 慢版(sleep 15ms,单跑 6.1s) | 伪失败 7/18(K=2:1/6,K=4:6/12) | 0/18 |

      控制组两边都 `all_passed=True`。失败形态两类,都会把判定翻成"没修好":
      - 测试中途 `FileNotFoundError`(先起步的执行,它的 `tmp_path` 文件被后起步者
        在 session setup 的 `rm_rf` 连带删掉)⇒ `rc=1 failed=1`;
      - 会话级 `errors=1`(被删的一侧连收集/收尾都做不完)⇒ `rc=1 passed=0 failed=0 errors=1`。
      快版比率反而**高于**慢版 ⇒ 竞争窗口就在"后起步执行的 `rm_rf`"这一刻,与测试跑多久无关,
      放大器只是把它放大到肉眼可见;结论不依赖放大器(判据里预先登记的那条)。

      机器默认临时根:执行前后编号目录 **4 → 4**,一次都没新增 ⇒ M11.5.0 的更正成立,
      走 `run_pytest` 的本地执行**不落机器根**(判据 (C) 不触发)。
- [x] M11.5.3 今天能不能被踩到(把"缺陷"和"事故"分开):主流程 verify 的 4 次 pytest 串行、
      Best-of-N 候选按 `cand{i}` 分目录、跨任务按 `run_dir` 分目录 ⇒ **当前无可达路径**。
      但有一条**今天已存在的**可达变体:超时的 pytest 被 `_kill_tree` 杀不干净时
      (`local_runner.py:144-156` 自己注释了"逃逸出进程组的分离孙子进程"),孤儿进程仍在写
      共享 basetemp,下一轮执行在同一个 `run_dir/reports/basetemp` 上 `rm_rf` ⇒ 同一任务内
      并发共享,踩中的就是刚测出的这个形态。
- [x] M11.5.4 暴露面(数出来的,不是推的):35 道宿主题里,被执行到的测试文件用到
      `tmp_path/tmpdir/tempfile` 的只有 **1 道(BUG-015)**;12 道容器题的临时根在容器内,与本缺陷无关。
      但暴露面不能只按现有语料算:平台定位是"诊断用户的任意本地仓库",
      那类仓库用 `tmp_path` 的比例远高于我们的合成题(BUG-015 就是唯一一道而它恰好红过)。
      同一套件里真正共用机器默认根的是 `tests/test_bugset.py:53` 的**裸 subprocess pytest**
      (基线校验不经适配器,不带 `--basetemp`)—— 这才是本夜 `os.scandir(pytest-of-wabcy)`
      PermissionError 的完整出处;`_isolated_nested_temp` 之所以有效就是因为它透传 TEMP。
- [x] M11.5.5 已落地(`a127b28`,2026-10-08 12:40):`run_pytest` 的临时根改成
      `<报告目录>/<junit 名>.basetemp-<8位随机>`,并在执行结束后自己回收。
      落地时纠正了两处我原来的说法:
      - "代价是每次执行多一个临时目录要随 run_dir 清理"——**pytest 对显式 `--basetemp`
        根本不做收尾清理**(`tmpdir.py` 的 finish 只走"没给 basetemp"那一支),所以不回收
        就是真的攒着;现在适配器自己 `rmtree(ignore_errors=True)`,判定用的 junit 与
        精炼堆栈都在报告目录里,与被删的目录无关。
      - "随机后缀只是防同名 junit 跨轮复用"不止如此:超时被杀的会话可能有**逃逸的孙子进程**
        还在写(`local_runner.py:144-156` 自己注释过),下一轮复用同名目录时删它的人会撞上
        孤儿持有的句柄 —— 实测旧行为下除了 `FileNotFoundError` 还见到
        `FileExistsError`(session setup 的 mkdir)与清理时的 `PermissionError` 两副新面孔。
      用例:`tests/test_basetemp_isolation.py` 两条(跑真子进程,被删的是文件系统事实)。
      **反向验证做过**:把 `_basetemp_for` 改回旧行为,并发用例 18 次里 12 次伪失败;新行为 0 次。

### M11.6 我自己在 M10.5 装的防线与 M0 的 addopts 冲掉了"文档里那条命令"(2026-10-08 11:55)

- [x] M11.6.1 事实:`pyproject.toml:26` 是 `addopts = "-ra --basetemp=.pytest-tmp"`,
      basetemp **在仓库内**(`.gitignore` 第 6 行收着);M10.5 的会话级防线
      `tests/conftest.py:55 _basetemp_outside_repo` 见到仓库内 basetemp 就整体报错。
      于是 `AGENTS.md:35` 与 `README.md:43/84` 写的 `pytest -q` 现在**必然失败**,
      而且是先 `rm_rf` 掉 `.pytest-tmp`(当前 408 个测试期仓库副本,含 `base-SWE-*` 缓存)再报错。
      实测:显式给仓库内 basetemp → `AssertionError: basetemp 在仓库内` + 整会话 error;
      给仓库外 → 正常 1 passed。我夜里那些全量跑都是显式传了仓库外 basetemp 才绿的,
      这件事当时没写下来,所以防线与配置的冲突被我的个人操作习惯掩盖了一整天。
- [x] M11.6.2 为什么不能简单地"删掉 addopts 里的 pin":这台机器的系统临时根**至今不可用**
      (PM-004 的 ACL 损坏没修)。实测 `-o addopts=-ra` 退回默认根 →
      `tests/conftest.py:63 PermissionError`;`icacls %TEMP%\pytest-of-wabcy` 直接"拒绝访问",
      同级还留着 `pytest-of-SYSTEM`(当年以管理员/SYSTEM 跑过 pytest 留下的)。
      所以 pin 不是随意加的,它是对一台坏机器的适配 —— 但它被写进了**仓库配置**,
      于是换任何人/CI 都会撞上,而防线又正确地拒绝了仓库内落点。
- [x] M11.6.3 按方案①落地(2026-10-08 12:45):`pyproject.toml` 的 addopts 去掉
      `--basetemp=.pytest-tmp`(留注释说明为什么不钉),`AGENTS.md:35` 与 `README.md`
      两处命令改成 `pytest -q --basetemp=D:/tmp/pt` 并写明理由(CI 仍用默认值),
      `tests/conftest.py` 的防线报错改成给得出路的文案,并把落点不可用的 `OSError`
      单独包住(以前是裸 `WinError 5` 站在那儿,看不出该做什么)。
      实测:裸 `pytest -q` 现在是**清楚的报错且不再先删 `.pytest-tmp`**;
      带仓库外落点 3 passed。PM-004 追加"后续"一节,把这次的反噬写进去。
- [x] M11.6.5 机器侧 ACL:非管理员能做的都试过了,**做不了**(2026-10-08 13:05)。
      实测:`%TEMP%\pytest-of-wabcy` 目录本身能 `dir`(空目录),但
      `icacls`/`ren`/`icacls /grant wabcy:(OI)(CI)F` 全部"拒绝访问" ——
      连安全描述符都读不到,当前令牌既无 WRITE_DAC 也无 DELETE。
      旁边 `pytest-of-SYSTEM` 的 ACL 是 `NT AUTHORITY\SYSTEM / BUILTIN\Administrators / OWNER RIGHTS`,
      即当年以 SYSTEM 身份跑过 pytest 留下的;pytest 用 `pytest-of-<getuser()>`,
      这个目录我们的运行**永远不碰**,属于纯残留,可留可清。
      留给你的两条命令(管理员终端,二选一;都是删临时产物,不影响任何项目源码):
      `icacls "%TEMP%\pytest-of-wabcy" /reset /t /c` 之后 `rmdir /s /q`,
      或直接 `rmdir /s /q "%TEMP%\pytest-of-wabcy"`。
      修好之后本机的 `pytest -q` 也可以不带 `--basetemp`(仓库侧已不依赖那个根可用:
      `a127b28`/`94d558b` 之后落点由调用方给,防线只拦"仓库内")。
- [x] M11.6.6 配置改动的连带后果已补:`scripts/measure_retrieval_domain.py` 原来只扫
      `.pytest-tmp/`,而落点搬出仓库后它会"如实报 0 题"——那是一种**假干净**。
      现在默认扫仓库根下所有 `.pytest-tmp*`、并支持 `--root=<basetemp>` 指仓库外落点,
      报 0 题时把扫过的落点打出来。复跑数字与 M13 一致(12 个缓存仓库、20 个必改文件、
      旧上限下不可见 5、域内缺失 0、全域最大位次 1168、AST 20/20 与 2263 符号)。

### M11.7 实现时顺手查出的潜伏坑(只登记,未动)

- [x] M11.7.1 事实:`run_pytest` 允许 `report_path=None`,此时 junit 与临时根都落在
      **工作区内**(`cwd/.patchpilot_junit.xml`、`cwd/.patchpilot_junit.xml.basetemp-xxxx`)。
      而 `app/gitops/differ.py:30` 用 `git add -A -N` 取 diff,未跟踪文件会进 diff
      ⇒ 原则上平台产物可能被算成"模型改过的文件"。
      **但今天有两道"恰好成立"的遮挡**,所以这不是一个会兑现的缺陷:
      ①`.patchpilot_junit.xml` 正好在物化仓库强制注入的忽略清单里
      (`app/gitops/testing.py:19` 的 `REQUIRED_GITIGNORE_LINES`);
      ②临时根现在被 `finally` 里的 `_discard_basetemp` 回收(git 只报文件不报空目录,
      所以旧写法在"没被回收"时也只是恰好不响)。
      剩下的真实缺口很窄:平台进程自己在 pytest 期间死掉 ⇒ 回收没执行、目录里有文件
      ⇒ 续跑后的 diff 视图会带上它们。要不要把 `report_path` 改成必填(或在 local 分支
      遇到 None 直接 `ExecError`)属于签名变更,等裁决;我倾向**不改**,只在文档里写明
      "默认值仅供测试用,生产调用方必须传报告目录"。
      **2026-10-08 落地:只做了"文档化"那一半** —— 上述口径写进 `docs/design.md` §8(已知边界),
      `app/adapters/pytest_adapter.py` 的签名与行为**一字未动**(禁区;必填化仍需你点头)。
      这条留着"不修"是**故意的**,不是遗漏,别被下一个人当欠账补掉。
- [x] M11.6.4 复跑确认(2026-10-08 12:20):`eb0e103` 之后全量 **706 passed / 3 skipped**
      (1142.24s;夜里那次是 841s,差的是机器负载,用例集合与判定字段没变),工作区无残留。
      这一轮的唯一行为改动是 `tests/conftest.py` 的 docstring 口径,数字不变即符合预期。
      注意这条绿是**手传 `--basetemp=D:/tmp/pt-full-m116`** 拿到的 —— 即 M11.6.3 里那条
      "用个人习惯遮住冲突"的老路,复跑本身不构成对 `pytest -q` 的验证。

### M11.8 换机接力清单(2026-10-08 13:20,写给另一台机器上接手的我/你)

- [x] M11.8.1 **代码与文档已全部在 origin**(`master` 与 `origin/master` 同步,无 stash,工作区干净)。
      但 `.gitignore` 把这些挡在仓库外,换机后**它们不在**,别当成丢了东西:
      `runs/`(全部批次产物,第 14 行)、`.pytest-tmp*`(测试期仓库副本与 SWE 上游仓库缓存)、
      `.env`(第 28 行,**含 LLM 密钥,从来不入库**)。
- [x] M11.8.2 新机要做的准备(按依赖顺序):
      ①`python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt`
        (`pytest`/`ruff` 只在 `.venv/Scripts` 里,系统里那些不能用);
      ②`.env` 从 `.env.example` 复制后填 `PATCHPILOT_LLM_API_KEY`(真实模型批的开关是
        `PATCHPILOT_LLM_ENABLED`,默认 false);
      ③要复现 M13 那组"位次"数字就得让 `rg` 在 PATH 上 —— `app/tools/search.py:75` 用
        `shutil.which("rg")`,找不到就按 `search_engine=auto` 退回 python 引擎,
        **检索遍历域的形状会变**,两机数字不可比;
      ④要跑 SWE 题需要重新 `docker pull`(9/10 道难题镜像只在这台机器上),
        以及 `docker/executor.Dockerfile` 的镜像构建。
- [x] M11.8.3 跑法:先直接 `pytest -q`。**这台机器的 `%TEMP%\pytest-of-wabcy` ACL 坏了且非管理员修不了**
      (见 M11.6.5),所以本机必须 `pytest -q --basetemp=D:/tmp/pt`;新机若默认临时根可用就不用传,
      防线只拦"落点在仓库内"这一种。全量基线数字:2026-10-08 13:00 实测 **708 passed / 3 skipped**
      (3 个 skip 是 Redis 连接与 Windows 符号链接权限这类环境依赖,不是被跳过的工作)。
- [x] M11.8.4 当场可复现的证据(全部零成本):
      `python scripts/compare_batches.py runs/graph35-v2 runs/night-final-2026-10-08`(两臂判定字段逐题相等)
      —— 但 `runs/` 不入库,新机没有这些批次目录,这条只能在这台机器上跑,或重新跑批再生成;
      `PYTHONPATH=. python scripts/measure_basetemp_contention.py`(并发临时根测量,不依赖任何缓存);
      `PYTHONPATH=. python scripts/measure_retrieval_domain.py --root=<basetemp>`(依赖 SWE 仓库缓存,新机先跑 corpus 定向用例);
      `pytest tests/test_resume_crash.py`(真·进程死亡续跑);
      `pytest tests/test_basetemp_isolation.py`(临时根并发互删的回归用例,反向验证配方在 PROGRESS 的 M11.5 卡)。
- [ ] M11.8.5 仍压着的裁决(别在新机自行推进):%TEMP% ACL 是否管理员清理、仓库根 `.pytest-tmp*`
      缓存清不清、M11.7 的 `report_path=None` 是否改成必填、真实模型批(换模型 / 抬预算 /
      压缩阈值与 PLAN 段的效果标定)、`metrics.py` 的 gold 结构对比、`@`/`=` 测试 id 白名单、
      `BUG-014` 的 reference.diff 存量缺陷、Docker 数据盘迁 D 的窗口、`nodes.py` 拆分。

### M12 补上缺失的那篇 ADR:补丁协议(2026-10-08 01:59)

- [x] M12.1 目标里"Patch/Diff 局部编辑而非全文件重写"是 ACI 层最核心的决定,但七篇 ADR 里
      没有一篇写它(只在证据/复盘文档里被引用过)。新增 `docs/adr/0008-补丁协议-块锚定局部编辑.md`:
      三候选对比、结论(块协议**编译到** unified diff 而非替代它,防线链零改动)、
      `_locate` 的三个拒绝细类、`split_keepends` 禁 `splitlines()`/`read_text` 的行尾保真理由,
      反方条目如实写:上下文重复的真实金补丁无法机械 round-trip(故这类题不入集)、
      rename/copy 与二进制不迁移、`ambiguous_anchor` 是一轮额外消耗、
      **协议相对裸 diff 的收益没有对照数据**(fake 照不出)。
- [x] M12.2 四条形如 `- \`path:line\` — \`子串\`` 的验证锚点按 `blockpatch.py` 实际行号写,
      `tests/test_docs_anchors.py` 6 例绿(逐条验真)。`docs/README.md` 索引同步"七篇→八篇"。
      本卡只新增文档,代码零改动。

### M13 真实语料复查遍历域修复(2026-10-08 02:23)

- [x] M13.1 新增 `scripts/measure_retrieval_domain.py`:拿 `.pytest-tmp/base-SWE-*/repo` 的
      **真实上游仓库**复查 D.12 缺陷与 M3.6 修复的单测盲区(形状对≠真实位次可达)。
      缓存不在场时如实报告 0 题,不进测试套件(不让全量绿依赖临时产物)。
- [x] M13.2 实测结果:12 题可测(9 题无缓存不算分母),必改文件 20 个在新遍历域内**全部可达**、
      最大位次 1168、无一题触到 8000 上限;其中 **5 个文件在旧的 500 上限下不可见**
      (sphinx-7590 cfamily.py=619、astropy-8707 card.py=648/header.py=666、
      sphinx-9461 inspect.py=675、pylint-6903 run.py=1168)—— 旧缺陷挡住过 4/12 题的必改文件。
- [x] M13.3 AST 检索在真实第三方代码上的表现:20/20 可解析、共 2263 个符号条目
      (`find_symbol`/`describe_file`/骨架共用 `ast_outline`,所以这就是模型能看到的集合)。

### M14 完成度对账查出的两处账目缺口(2026-10-08 02:27)

- [x] M14.1 `app/context/ast_outline.py` 一直没有**自己的**用例:渲染侧被 `test_repo_map.py`
      间接钉着、结构化侧被 `test_search_tools.py` 经工具间接覆盖,而"骨架集合 == 工具集合"
      这条模块立身不变量没有任何用例守。新增 `tests/test_ast_outline.py` 10 例:
      逐项相等(名字+顺序+签名+行区间)、局部函数两侧都不出、类嵌套只再进一层
      (`TooDeep` 不出)、kind 三分、装饰器/async/返回标注,以及 `parse_source` 的
      **"永不抛出"契约**(半截文件/空字节/括号深渊/错缩进,只断言 (树为空) ⇔ (有错误串),
      不钉解释器异常名 —— 400 层括号在 3.11 是 RecursionError、3.12 是 SyntaxError)。
- [ ] M14.2 待裁决(改它等于改模型每次请求看到的东西,不自行翻):大纲里的**签名不带参数类型标注**
      (`def top(x: str) -> bool` 渲染成 `def top(x) -> bool`)。这是移植前就有的口径,
      取舍是骨架按仓库规模付 token;但"空输入返回 None"这类缺陷的线索常常就写在参数标注里。
      可选做法:骨架有余量时保留标注(`ast.unparse(args)` 形态)、或只对被检索命中的文件用
      `describe_file` 出全标注。需要一次真实两臂对照才能定,不靠拍脑袋。

### M15 真实语料量上下文机制的额度效果(2026-10-08 13:35,零成本、不起模型)

背景:M1–M14 的每个机制都只证到"接线不破行为",对外文档里"未证明"那一条一直挂着,
理由是"fake 语料只有 7-9 轮,够不到 16000 阈值"(M1.5/M8.1/ADR-0004)。
但 `runs/swe-*` 里躺着 **25 份真实模型 run**(13 FINISHED / 11 BUDGET_EXCEEDED / 1 NEEDS_REVIEW,
跑在升级前 commit `80981f0`,provenance 无 `context_window_tokens`、轨迹里 `context_compact` 事件 0 次
⇒ 它们是**压缩未介入**的干净前对照语料)。这批数据没被用过一次。

- [x] M15.0 可行性先证再主张:轨迹里 `llm` 事件存 content+tool_calls+**provider 报的逐轮 tokens**,
      工具事件存 fold **之前**的原文;`read_file`/`search_code` 的入参与回执完整,
      所以逐轮请求消息列表可以按 `plain_loop` 的构造规则重建(system/user 从
      `git show 80981f0:app/prompts.py` + `bugs/<id>/` 题目夹具取,当时 LOCALIZE 不传 extra_system)。
      已核实的记录形态限制:`fold_output` 在 JSON 上退化成 8000 字符硬截(JSON 无真空行)、
      `_summarize_input` 把 >300 字符的字符串入参截断(所以 apply_patch 正文只剩 300 字符 + 真实长度标记)、
      `_summarize_output` 只留 output 的前 8 个键、`git_diff` 的 diff 换成 `<N chars, see patches>` 占位。
- [x] M15.1 `scripts/context_replay.py`(A1):语料装载 + 逐轮重建 + 三道**跑前写死**的自检。
      实测:**F1 守恒** 24/25 逐字相等,1 条例外已单独归类
      (`swe-hard-oneshot/SWE-sphinx-doc__sphinx-7748` 轨迹 55,526 而 report 记 0 —— 那正是
      `4a4093e` 修的"崩溃路径不入账"缺陷实物,既不算装载失败也不算通过,脚本里写明);
      **F2 动作序列对齐** 0/251 错配(251 = 被重放的 LOCALIZE 回合;全语料 367 回合);**F3 重建可用性** R²=0.9424、中位相对误差 13.4%(门槛 0.90/15%)
      ⇒ GATE PASS。记录形态必然少算的字符:32,093 chars = 语料估算体量的 0.35%。
- [x] M15.2 **估算器标定**:`app/llm/base.py:47` 的 `len(text)//4` 在真实语料上**低估约 1.47 倍**
      (逐实例 real/est 中位 1.470,p10 1.283,p90 1.697,区间 1.263~1.897,n=25)。
      两个直接后果:①`context_window_tokens=16000`(估算口径)对应的真实上下文是
      **≈23,500 tokens(20,500~27,200)**,不是一个 16k 窗口的东西;
      ②`plain_loop.py:305` 的门禁是**混单位**的——累计量用 provider 真值、
      待发的那一次请求用 `len//4` 估算,所以它系统性**晚**判死(误差只有一个请求的 47%,
      不随轮数累积,量级约 1~15k 真 token)。这条不是" bug 报告",是要不要统一口径的裁决。
- [x] M15.3 `scripts/measure_context_counterfactual.py`(A2):静态动作序列反事实
      (假设模型行为不变,只把工作记忆换成压缩后的版本)。
      **V1 锚点(有牙齿的可信度检查)**:额度型判死的 error 原文带 `agent loop tokens X exceed budget Y`,
      而 X = 已耗真值 + 代码当时算出的估算值,于是 `X − Σ真值` 就是**同一口径**的真实估算读数,
      零换算直接对撞重建 —— 实测 2/2 命中:29,679 vs 29,426(0.9%)、23,838 vs 23,638(0.8%);
      **负对照**:故意丢掉工具回执的重建只给出 1,613 / 1,411(差 18~20 倍)⇒ 这个锚不是摆设。
- [x] M15.4 阈值表现(25 个 LOCALIZE 实例,估算口径):生产默认 **16000/keep=6**
      触发 11/25 实例、首次触发中位第 12 回合、累计压缩事件 26 次(60 条 tool 消息存根化、
      18 条整组丢弃),**触发实例的中位额度节省只有 6%**(全体实例中位 0%);
      省得最多的是最长的那几个 run(25%/24%/26%/18%)。
      想省到 31% 得把阈值压到 8000,但那时 **86%(keep=6)/100%(keep=8)/52%(keep=4)的压缩回合压不到阈值**
      (分母是"调用过压缩的回合",含"压到底了也没变小"的那些;钉住的最近平回合本身就比阈值大)——
      "阈值"和"保留回合数"是联动的,单独调阈值的那个 16000 不是一根可用的杠杆。
      生产默认 16000/keep=6 的压不到位率只有 4%,也就是说**它现在基本压得到,但省得也少**。
- [x] M15.5 **判死原因分类(这条直接改变"抬 token_budget"的意义)**:11 次 BUDGET_EXCEEDED 按
      error 原文拆开 = `localize/token-gate` 2、`propose/token-gate` 2、`one_shot/token-gate` 1、
      **`one_shot/max-turns` 6**(max_turns=12 四个、=6 两个)。
      ⇒ **与额度有关的是 5 次**(定位段 2 / 补丁段 2 / one-shot 段 1),压缩在 16000 下能让
      其中 2/2 次定位段判死跑完原动作序列不撞墙(n=2,且不等于"能修好")。
      另外 6 次的约束是**回合数**,而且**全部落在 `runs/swe-hard-oneshot` —— 就是我在 `f4a128e`
      作废过的那批**(私加轮次上限导致 0/7 空结果)。这 6 次既不能用来论证"抬预算有用",
      也不能用来论证"压缩能救",它们只说明一件事:**status 名字不等于死因**,
      `BUDGET_EXCEEDED` 里有 55% 压根不是 token 问题。
      ⇒ 排除作废批(整批排除,不挑样本;这条判据在拆之前就写死)之后,**可用样本里的 5 次判死
      全部是额度闸** —— 压缩/预算正是该测的杠杆,同时 n=5 也说明"抬预算赌一次"仍然不是设计。
      分类这一步零成本,应该在付费之前做。
- [x] M15.6 **锚点安全 + 请求合法性**:对每题金补丁(`bugs/<id>/expected/reference.diff`)的文件清单,
      逐实例比"不压缩时最后一次请求看到的" 与 "压缩后看到的",分母固定(全语料 43 个金补丁文件项,
      其中 1 项不压缩也没读到 ⇒ 42 项"本来全文可见",这个分母不随阈值变)。
      16000/keep=6:41 仍全文可见、**1 个连路径都没了**、0 个降级成存根;
      8000/keep=6:38 全文 / 1 存根 / 3 丢失。⇒ ADR-0004 当年"摘要可能丢掉补丁要的锚点"这条顾虑
      在**当前生产阈值上是 1/42**,不是零;阈值降到 8000 就变成 3/42(+1 降级)。
      另外 12 个压缩单元格(4 阈值 × 3 keep)累计 **0 条孤儿 tool 消息** —— 压缩后的请求形态在真实轨迹上始终合法。
- [x] M15.7 证据入库:`docs/evidence/2026-10-08-context-corpus-calibration.txt`(A1 原始输出)
      与 `docs/evidence/2026-10-08-context-counterfactual.txt`(A2 原始输出)。
      复现:`PYTHONPATH=. .venv/Scripts/python.exe scripts/context_replay.py`
      / `scripts/measure_context_counterfactual.py`(依赖 `runs/` 不入库,只能在这台机器上跑)。
      **为什么没有 pytest 用例**:两脚本的输入是 `runs/` 里的批次产物,而 `runs/` 在 `.gitignore`
      第 14 行 —— 让全量绿依赖它就等于制造一条"换机器就假通过/假失败"的边(与 M13 同一口径)。
      替代钉子是**脚本自身带退出码闸**:A1 的 F1 不守恒即 exit 1、A2 的 V1 锚点未全中即 exit 1,
      判据写在文件头部,改判据必须同时改那段文字。
- [ ] M15.8 **仍未证明(不许对外写成效果)**:以上全是**额度/形态**证据。
      ①静态反事实假设动作序列不变,而真实模型看到更短上下文可能少查也可能重复查;
      ②PROPOSE 段那 2 次额度判死**没进反事实**(该段头部需要 `build_feedback` 与上轮 findings,
      按当时版本重建的风险大,我只重建了 LOCALIZE 头部并把未覆盖的 9 次判死如实列出);
      ③修复率、以及"压缩/骨架/PLAN/反思是否让结果变好"仍然只能由真实两臂批给出。
      下一步该跑什么批次,由这些数字来定(见 M15.9)。
- [ ] M15.9 待裁决(需要真实模型批,不自行发起):阈值 16000 是否值得改到"额度优先"(8000/keep=4)
      还是保持"锚点优先";混单位的预算门禁要不要统一成 provider 真值口径;
      以及付费批的题单/判据(C 卡)。**M15 只提供定价信息,不代替裁决。**

## 明确不做(需用户裁决,不自行推进)
- **LLM 语义摘要**(目标里"语义摘要"的一支):ADR-0004 已把它列为"考虑后否决"的候选
  (多一次花费 + 摘要可能丢掉补丁要的锚点上下文),现落地的是**机械压缩**。
  若要改做,建议形状:只对 Pass B 即将丢弃的整组 turn 发一次 summarize 请求、把结果作为
  一条 pinned system 附加消息,并用 `context_summary_enabled` 默认关闭 + provenance 登记;
  代价是每次压缩多一次模型调用(付费,需授权)与一次新的不确定性来源。
- ~~M11.2 的两条候选修法(改 `pytest_adapter` 命令或执行器 env 白名单)~~
  **已于 2026-10-08 12:40 裁决并落地(`a127b28`,方案①的"每次执行私有"形态)** —— 划掉是为了不让你
  或下一个我把它读成"还压着的事"。留此划线记录而不是删行:它当时确实是"必须掌握"区的语义变更、
  确实等了点头才动。测量与反向验证见 M11.5 卡。
- 任何真实 LLM 付费跑批(含换模型、抬 `token_budget`、难样本补齐两臂)。
- `app/evals/metrics.py` 的"与 gold 结构对比"新判定语义;测试 id 白名单接 `@`/`=`;`PATCHPILOT_PRICE_OVERRIDES`。
- `bugs/BUG-014/expected/reference.diff` 存量数据缺陷修复;Docker 数据盘迁 D(需停 mysql/redis/qdrant)。
