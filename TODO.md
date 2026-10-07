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
- [ ] M1.5 按 M7.1 的零成本回放实测给 `context_window_tokens` 定**生产默认值**;
      长期停在 0 = 机制没上线,这一项不做完 M1 不算收口。

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
- [ ] M3.6 **检索遍历域与输出体量解耦**(新发现,证据见 PROGRESS.md D.12):`_iter_repo_files` 的
      `MAX_LIST_FILES=500` 同时裁掉了 `search_code`/`find_symbol` 的遍历域,实测 9 题里 4 题的
      金补丁文件排在第 500 个之后(sphinx-7590 的 `sphinx/util/cfamily.py`=631、
      scikit-learn-12682 的两处=502/903),即**必改文件对模型不可见**。
      改法:`list_files` 保留 500 条**输出**上限(带 truncated),`search_code`/`find_symbol`
      改走独立的高上限(新 Settings 键,默认覆盖整仓);存量等价性用例与两臂口径不得因此改变。
- [ ] M2.5 骨架在大仓库要出**目录级结构**(目录 + 文件数 + 深度上限)而不是"字母序前 200 个文件":
      现口径下 sphinx/astropy 仓库拿到的是一张只画了角落的地图。

### M4 反思机制升级(廉价且高价值)

- [ ] M4.1 失败轮把 rollback 前保真的 diff 摘要(文件 + 每文件增删行数 + 关键 hunk 头)拼进下一轮反馈,
      让模型知道"上一版改了什么、为什么没生效"——当前 rollback 后模型对工作区已无痕迹。
- [ ] M4.2 连续同签名失败时,要求反馈里带"必须改变的假设"清单(仍是文本层,不动门禁)。
- [ ] M4.3 用例:diff 摘要形状与上限;首轮回滚不污染;repeat_streak 计数口径一字不动。

### M5 严格的 Locate→Plan→Act→Verify

- [ ] M5.1 `TaskState` 增 `plan` 字段(可序列化,进 checkpoint)。
- [ ] M5.2 `builder.py`/`nodes.py` 在 localize 与 propose 之间插 `plan` 节点:一次结构化调用产出可执行计划
      (目标文件/符号、改动意图、预期验证),计划文本被 pin 进 PROPOSE 上下文并进轨迹;VERIFY 失败时回流 PLAN 修订。
- [ ] M5.3 `Settings.plan_stage_enabled`(默认开;置 false 回到三段时间线),plan 调用走既有预算/时间/取消检查。
- [ ] M5.4 用例:开关两态图形状、plan 为空/超长、预算耗尽时 plan 段降级继续(与 LOCALIZE 同构)、fake 回放 35 题零回归。

### M6 状态持久化与中断恢复(checkpointer 从"留档"变"可续跑")

- [ ] M6.1 循环层 turn 边界快照:落 `run_dir/loop_state.json`(压缩后的消息列表 + 阶段/轮次/turn/已耗用量计数),尺寸上限保护。
- [ ] M6.2 `runner` 增恢复入口:读 checkpoint state + 循环快照 → 重建 ToolContext → 从中断阶段续跑;
      快照缺失/损坏/版本不符 → 退回"阶段重跑",落取证事件。
- [ ] M6.3 `service.recover_stale`:有可续跑快照的任务改为重新入队续跑(而非一律 NEEDS_REVIEW);
      不可续跑的保持现口径。**硬约束**:恢复后 APPLY/VERIFY/门禁必须真重跑,绝不复用中断前的"已过闸"结论。
- [ ] M6.4 用例:turn 边界 kill 后续跑且阶段不重做;快照损坏安全降级;门禁在恢复路径上重新执行;重复续跑幂等。

### M7 证据、文档与收口

- [ ] M7.1 零成本对照回放:M1(压缩开/关)、M2(骨架开/关)、M5(plan 开/关)各自的 avg_tokens/turns 差异报告。
      测量工具已就位:`scripts/compare_batches.py <基线批> <对照批> [--tools]`
      (判定字段逐题不等即退出码 1;成本字段只报差异;`--tools` 出逐阶段工具调用直方图与
      `context_compact` 事件数)。基线批 = `runs/graph35-v2`(35 题,合计 turns 249 / tokens 6917)。
- [ ] M7.2 ADR:上下文分层与压缩策略、检索工具从子串到 AST/ripgrep、续跑语义与"闸必须重跑"。
- [ ] M7.3 `docs/design.md` 与 README 对齐实际实现;PROGRESS.md 记录每个 commit 的机器证据。
- [ ] M7.4 全量 `ruff check . && ruff format --check . && pytest -q` 收绿 + graph 35 题回放对基线零回归。

## 明确不做(需用户裁决,不自行推进)

- 任何真实 LLM 付费跑批(含换模型、抬 `token_budget`、难样本补齐两臂)。
- `app/evals/metrics.py` 的"与 gold 结构对比"新判定语义;测试 id 白名单接 `@`/`=`;`PATCHPILOT_PRICE_OVERRIDES`。
- `bugs/BUG-014/expected/reference.diff` 存量数据缺陷修复;Docker 数据盘迁 D(需停 mysql/redis/qdrant)。
