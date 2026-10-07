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
  定向 83 passed;全量见下一条 commit 前的 `pytest -q` 结果(基线 536 → 本批新增 21 例)。

