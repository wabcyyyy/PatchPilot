# ADR 0014:模型能力档案与 repair_v2 上下文策略 —— 声明、观测与愿望分开记账

日期:2026-10-11 | 状态:已采纳 | 素材:SPEC §5、§7 G07、验收矩阵 T12,评审 `agent-readiness-review-2026-10-10.md` R3,`docs/adr/0012`(G05 遗留的联动标定)、`docs/adr/0004`(16k 软窗口的推导)

## 背景

升级前配置里只有一个与容量有关的数字:`model_context_tokens=0`(G03 加的,0 = 不比较),
**没有任何地方回答"这个数字是从哪来的"**。于是三种东西共用一条命名带:

- `model_context_tokens` = 端点声明的单请求硬容量;
- `context_window_tokens` = 工作记忆的软压缩目标(16k);
- `token_budget` = 任务累计额度(200k);
- `llm_max_tokens` = 单次输出上限(4096)。

前两个只差两个词,读配置的人很容易把"我们打算压到 16k"当成"端点只有 16k",
或反过来把 200k 累计额度当成单次能装 200k。SPEC §5 把资源切成四类且明令**互不替代**,
§7 G07 要求"设置项名称必须能区分这些概念"。

另一条是未验证窗口的代价。模型名里带 `128k` / `1M` 一旦被认为依据,平台就会按大窗口装配
上下文;真实保护区(系统约束 + 首条 issue + 尾部钉住的回合组)超出端点容量时,
循环会**每轮压缩一次、每轮再撞一次超长**,直到把额度耗光 —— SPEC §5 正是针对这个写了
"不得反复压缩直至耗尽预算"与 `context_capacity_exceeded`。

同批遗留还有两笔:G02 造好的 `TransportRetryPolicy` 一直没有生产调用方;
G05 明确登记"抬软窗口到 64k 时必须把 `refine_head/tail_lines`、`task_memory_max_chars`、
`repo_map_max_chars` 一起标定"。

## 候选

1. **按模型名查表推断能力**(拒绝):名字不是合格证,灰度改名就悄悄改变窗口与判据,
   报告里看不出运行条件变过;SPEC §5 要求"能力只能显式声明"。
2. **把默认软窗口直接抬到 64k**(拒绝):等于替每个端点宣称支持 64k,而离线既不能证实
   也不能证伪;§7 的退出条件写的就是"离线通过不推出 64k 的真实效果最优"。
3. **显式声明 + 来源 + 保守档 + 每任务冻结的 profile + 请求前判据**(采纳)。

## 结论

- **声明分四种来源,没来源就是假设**。`app/llm/profiles.py` 的 `ModelProfile` 带
  `capacity_source ∈ {operator_config, provider_docs, probed, unverified}`;`unverified`
  只能落在保守档 `UNVERIFIED_CAPACITY_TOKENS=16_000`,**申报比它大一律拒绝发起**
  (不"先按大窗口跑跑看",也不悄悄改成 16k —— 那等于替未验证端点背书)。
  有来源却没给容量数字同样拒绝:那是一条看起来已验证的假声明。
- **能力清单只认显式 yes/no**:`model_capabilities="tools=yes,reasoning=no"`;
  没写的项留 `None` 并进 `assumed` 清单一起落盘,报告能看出"哪几项是平台替端点假设的"。
  拼错项点名拒绝 —— 坏字符串静默当成"没声明",就会把"这端点不会调工具"读成"应该会"。
  `tools=no` 的端点在构造处被拒:没有工具调用就没有检索、补丁与测试反馈。
- **观测不进声明**。端点回报的 `served_model` 挂在 `ModelResponseEnvelope`(逐次响应),
  与请求名不一致就标 `model_identity_drift` 并进轨迹;`provenance.model_identity()` 里那个
  字段仍然如实留 `None`,因为它是**发起前**的登记位。名字相同也不能证明服务端版本没漂移
  (G04 的 `alias_proves_served_version=false` 原样保留)。
- **四类资源在 `ContextPolicy` 上各有各的名字**:`soft_compaction_target_tokens` /
  `cumulative_token_budget` / `completion_output_cap_tokens` / `capacity_margin_tokens`,
  硬容量归 `ModelProfile`。Settings 键名一律不动 —— 改名就是改写历史实验身份(§7 G07)。
- **`legacy_v1` 是今日逐字**:四个数全部照搬 Settings,并且**不做容量比较**。
  默认 profile 名仍是 `legacy_v1`:SPEC 要求新默认切换等到 G16 收口。
- **`repair_v2` 只在能力允许时采用目标窗口**:目标 64k / 尾部钉 12 轮是工程起点,
  实际采用 `min(目标, 声明容量 − 输出预留 − 容量余量)`,被夹过就写进 `WindowResolution`
  与 TaskSpec。它同时要求显式输出上限(`llm_max_tokens=0` 在 v2 下被拒),
  并按端点声明的输出上界校验请求参数。
- **保护区判据排在压缩之前**:`protected_region()` 给出压不动的那一段(头部 + 尾部钉住的
  回合组,含未完成的 tool-call 配对),循环用 `estimate_request` 量它(含有效 schema 与协议
  开销,与账本同一把尺子),超过可用余量就一次 `context_capacity_exceeded` 收场并落事件。
  localize/plan 的"份额用尽可降级继续"对这一条**不适用**:换阶段还是同一个端点、
  同一份压不动的历史,继续只是再烧一次请求。
- **profile 是每任务冻结的实例**:`TaskNodes.__post_init__` 与 plain 引擎的任务起点各建一次,
  四个阶段与两臂消融共用同一份读数(`**self.runtime.loop_settings()`),不再每次回读全局
  Settings;读数同时落轨迹事件 `runtime_profile` 与 TaskSpec 的 `effective_policy.runtime_profile`。
- **恢复认得 profile**:`LoopSnapshot` 加第五个可空字段 `runtime_profile`,`binding_mismatch()`
  比它 —— 两侧都有绑定且不一致就拒绝续跑转待审。旧快照不记 profile(空串)⇒ 不参与比较,
  与 G06 同一条"不许把没绑定判成不一致"的规矩。
- **重试归属接上生产**:`Settings.llm_platform_retries`(默认 0 = 与今日逐字相同)装配
  `TransportRetryPolicy`;非 0 仍要求 `llm_max_retries=0`,否则构造处拒绝(两层重试的用量
  无法对账)。SDK 上界非 0 时照旧如实记 `retry_owner=sdk_opaque`。

## 代价与反方

- **64k/12 是拍的起点值,不是结论**。离线全绿只证明判据与接线;这个档位对这个端点这批题
  是否最优,只有 G17 的真实探针能主张。32k/64k/128k 是后续可比较档位,不在这里选边。
- **保守档数字(16k)是我拍的**。SPEC 只说"明确标为 unverified 的保守配置",没给数。
  取 16k 的理由是它等于 legacy_v1 已在用的软窗口 —— 也就是"不因为引入了声明层就替未验证
  端点多要一分容量"。换成 8k 也合法,但它必须是写死的假设而不是查表猜来的"能力"。
- **未验证端点上开 `repair_v2` 大概率第一次请求就被拒**:64k 目标被夹到 10,880,
  而 `keep_recent_turns=12` 的尾部往往就超过它。这是"不宣称支持"的直接代价,
  处置办法只有两条:给出有来源的容量,或者别开 v2。留一个"看起来能跑"的中间态更糟。
- 保护区判据每 turn 多一次 `estimate_request`(O(消息数))。**未声明容量时短路返回 0**,
  所以 legacy_v1 与今天的运行形状逐字相同、零额外开销。
- `context_capacity_exceeded` 现在收敛成 `BUDGET_EXCEEDED` 终态(资源类停止),
  不是独立终态。理由:它同样由"请求前的资源判据"产生,而独立终态要动状态机路由与验收面。
  与"平台没跑起来是否做独立终态"那条待裁决一起,留给 G16/G09 收口。
- **恢复时 profile 取自当前 Settings,不取自 spec 里那份**。我用快照绑定把错配挡在拒绝续跑
  这一侧(而不是让它默默用另一套口径跑完),但"按受理时冻结的 profile 重建 runtime"要改
  受理→构造链,登记给 G16。旧任务(快照里没有 profile)不受影响。
- **G05 登记的联动标定没有在这里做**:`refine_head/tail_lines`、`task_memory_max_chars`、
  `repo_map_max_chars` 与 64k 窗口的配比需要真实端点的读数才能定,离线定就是抄数字。
  现在抬窗口只改了软阈值,单条回执仍然由 `refine_*` 决定行数 —— 一屏能装更多行,
  但每条回执还是 55 行。
- 每个 Settings 键都要归类:新增 5 键(`llm_platform_retries`、`model_capacity_source`、
  `model_capabilities`、`model_declared_output_ceiling`、`agent_runtime_profile`)全部进
  `SNAPSHOT_KEYS`;其中重试与能力清单另进 `protocol_identity`,容量来源/输出上界/profile 名
  **不进**控制身份 —— 它们是额度与窗口旋钮,v1/v2 对照正需要按臂登记它们(与
  `context_window_tokens` 同一处置)。

## 验证锚点

- `app/llm/profiles.py:1` — `模型能力档案(G07)`
- `app/llm/profiles.py:37` — `UNVERIFIED_CAPACITY_TOKENS = 16_000`
- `app/llm/profiles.py:145` — `def assert_usable_for_repair`
- `app/agent/runtime.py:1` — `运行 profile(G07)`
- `app/agent/runtime.py:44` — `REPAIR_V2_SOFT_TARGET_TOKENS = 64_000`
- `app/agent/runtime.py:151` — `def _build_repair_v2`
- `app/llm/usage.py:1` — `模型响应与计量协议(G02)`
- `app/llm/openai_client.py:1` — `OpenAI 兼容端点的模型封装`
- `app/context/token_window.py:1` — `工作记忆的滑动窗口压缩`
- `app/graph/plain_loop.py:625` — `if capacity_ceiling > 0:`
- `app/graph/nodes.py:1` — `LangGraph 节点实现`
- `app/graph/loop_state.py:74` — `runtime_profile: str = ""`
- `tests/test_model_profiles.py:1` — `G07a 模型能力档案与服务端身份采集`
- `tests/test_context_wiring.py:1` — `上下文机制在**图路径**上的接线验证`
