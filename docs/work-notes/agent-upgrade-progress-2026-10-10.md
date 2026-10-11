# PatchPilot 修复 Agent 升级:执行进度(批次 B0 起)

任务卡定义在 `docs/PatchPilot修复Agent升级SPEC-2026-10-10.md`。本文是执行侧唯一进度源:
每卡记录 commit、改动语义、最小行为证据、检查读数、环境限制与未运行项。**不以勾选代替证据**。

---

## G00 / P0:核验基线,建立执行收据

- **commit**:`7b2a92c`(文档分层入库)、`6842099`(锚点路径存量修复);测量身份 HEAD `6842099`。
- **产出**:`docs/evidence/agent-upgrade/baseline.json`、`docs/evidence/agent-upgrade/probe-before-g01.json`、本节。
- **允许范围**:进度与证据文档;业务代码零改动(`git diff 876ff6f..6842099 -- app/ scripts/` 只含
  上一轮已存在的文档路径默认值改动,即 `app/evals/report.py` 的输出路径,由用户工作树原样带入)。

### 代码身份与分支决定

| 项 | 读数 |
| --- | --- |
| cwd | `D:/wcy/project/PatchPilot` |
| 交接时 HEAD / 分支 | `876ff6f` / `master`(非 detached) |
| 是否新建 `codex/repair-agent-b0` | 否。SPEC 与交接只在 detached HEAD 时要求新建分支;用户裁定本批在 master 上做,且未提交文档先单独提交 |
| 开工 dirty 文件 | 55 项,全部是上一轮 docs 分层重组的连带改动,原样提交在 `7b2a92c`,无丢弃 |
| 需保留的既有未跟踪工件 | 评审 md / 离线探针 py / 探针 JSON / 新 SPEC / 交接提示(清单见 baseline.json),已入库且不参与后续卡"成果"计数 |

历史评审基线 `d1bf0aae` 来自另一工作树,**不是**本项目版本;本收据的全部读数都取自 `6842099`。

### 环境(先复查再用)

- 解释器 `D:/wcy/project/PatchPilot/.venv/Scripts/python.exe` = **Python 3.14.2**(Windows 11 x64)。
- 该 venv **没有 pip 模块**(uv 管理)。`python -m pip show ...` 一律空返回,别把它读成"缺依赖";
  版本用 `importlib.metadata` 取:pytest 9.1.1 / ruff 0.16.7 / pydantic 2.13.5 / pydantic-settings 2.15.0 /
  langgraph 1.2.11 / langgraph-checkpoint-sqlite 3.1.1 / openai 3.14.0 / fastapi 0.141.1 / redis 8.1.0 / qdrant-client **缺**。
- `rg` 在 PATH 上(来自 Qoder 插件目录),CI 的 ubuntu runner 上没有 ⇒ 检索遍历域跨机不可比(旧账 M11.8.2)。
- basetemp 用仓库外可写落点 `D:/tmp/pt-upgrade-b0-baseline`;交接给的 `D:/tmp/pt-upgrade-b0` 实测同样可写。
  仓库内落点会被 `tests/conftest.py` 的会话级防线当场拦红(AGENTS 红线),不许"顺手"改回。

### 检查读数

```
.venv/Scripts/ruff.exe check .            → All checks passed!                     (exit 0)
.venv/Scripts/ruff.exe format --check .   → 233 files already formatted            (exit 0)
.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b0-baseline
                                          → 880 passed, 4 skipped, 4 warnings in 1562.30s (exit 0)
```

- 880 条通过是**本批实测的全量结果**,不引用评审文档里那条历史的 54 通过读数。
- 4 条 skip 全是环境依赖,列在 baseline.json(符号链接权限 ×2、Redis 不可用 ×2),没有一条是被跳过的
  工作,也没有为消掉失败而加的 skip。
- 后台运行的 exit 0 不作结论,只认自己重定向日志 `D:/tmp/pt-upgrade-b0-baseline.log` 的终态行
  (控制台按 GBK 码页输出,读数需按二进制解码取)。

### 立卡时查出的存量红(产品失败,非环境失败)

文档分层重组把 `design.md`/`docker-isolation-notes.md` 移进 `docs/archive/`,锚点没跟着走:

- `tests/test_docs_anchors.py::test_adr_declared_anchors_hold`、`::test_p3_7_corrected_claims_are_pinned`
  当场 `FileNotFoundError` → 修复前 **2 failed / 4 passed**,修复后 **6 passed**。
- 更要紧的一条:那两条 ADR 锚点之外的 **P3-7 假命题黑名单作用域**也指到了不存在的路径,
  等于"带来崩溃恢复"和"攻击样例(8 个"这两道防复发拦截被**静默取消**(测试不会红,覆盖面却没了)。
- 处置:只换路径,期望子串与被钉句子一字未动(`docs/adr/0001`、`0002` 的锚点行 + 用例里的作用域字符串),
  见 `6842099`。这是 B0 的范围外存量修复,已按"修复先行、与功能分 commit"落。

### SPEC §2 要求"由 G00 复核"的结论(在 `6842099` 上重测)

- **评审的 R1 仍成立**:`PYTHONPATH=. .venv/Scripts/python.exe docs/work-notes/audit_agent_review_20261010.py`
  在本代码上跑出 `plan_plain_text = {calls: 3, error: "agent loop exceeded max_turns=3"}` —— 连续三条
  合规纯文本计划仍被循环当成"没结束",烧到轮次上限。
- **R2 的可见 schema 仍成立**:`plan_visible_schema_count = 10`。PLAN 的白名单只有 `finish`,
  但 `plain_loop.py:337` 无条件发全量 schema。附带查到:`reset_workspace` 注册在案却从未进过
  任何阶段白名单(READ/WRITE/ONE_SHOT/两臂 policy 实测均无)⇒ 模型每阶段都能看见它、每用必被挡。
- **S00–S11 的落地件全部在场**,不重做:TaskSpec/共享验收/测试身份/输入冻结/preflight/资源账本/
  两级恢复/候选冻结/同引擎两臂策略(逐项命中见 baseline.json)。
- **Best-of-N 的接线限制仍成立**:`branch_model_factory` 在 `nodes.py`/`runner.py`,
  `app/api/service.py` 里没有这个名字 ⇒ API 任务不触发分支(G13 的范围,本批不动)。

### 费用与外部动作

零模型调用、零付费、零 push、零发布、零删除历史结果。探针脚本全程 Mock 请求,输出写在仓库外。

### 下一卡

G01(修复 PLAN 完成协议与可见工具权限)。

---

## G01 / P1:修复 PLAN 完成协议与可见工具权限

- **开始身份**:`feb59d2`(G00 收据之后)。**结束身份**:代码与用例一个 commit、本进度与证据一个
  commit,两条都在 `git log --oneline` 顶部(`git rev-parse HEAD` 即本卡的落地 commit)。
- **允许的语义变更**:SPEC §3.2 明列的"明确 PLAN 结束协议 / 能力对应 schema",交接提示并
  显式授权在 `app/graph` 这类必须掌握区按规格修协议,不再就同一语义重复请示。
- **未做**:窗口、总预算、阶段份额、价格、默认执行后端、实验参数一个字没动;
  `openai_client.py` 未改(空 tools 的省略行为本来就有,新增用例把它钉住,计量留给 G02);
  `experiment_policy.py` 未改(两臂的 PLAN 能力集合相同,没有可登记的新差异——加字段等于造
  一个没人消费的登记面)。**这是本卡的范围决定,不是遗漏。**

### 改了什么语义

| 文件 | 语义 |
| --- | --- |
| `app/graph/plain_loop.py` | 新增 `complete_on_text`(默认 False = 旧形状逐字不变):合规纯文本一次请求即结束;`MAX_PROTOCOL_REPAIRS = 1` 的有界协议修正;不合规则 `LoopOutcome.completion="protocol_invalid"` 带 `protocol_reason`;可见 schema 改按 `allowed_tools` 装配 |
| `app/tools/registry.py` | `tool_schemas(allowed_tools=None)` 只装配本阶段能力;白名单里出现未注册名字**当场 KeyError**(静默少发一个 schema 比崩更难查);删掉没人用的 `include_finish` 参数 |
| `app/graph/nodes.py` | `plan` 节点开启文本完成契约;`protocol_invalid` → 落一条 `plan_invalid` 轨迹事件后**降级继续**,终点集合仍只有 NEEDS_REVIEW / BUDGET_EXCEEDED |
| `app/prompts.py` | PLAN 提示写明"完成方式=把这 4-6 行作为纯文本发出";新增 `TEXT_COMPLETION_REPAIR`(只重申协议,不替模型写内容) |
| `app/llm/fake.py` | `plan_mode="legacy"`(默认,marker 免步 + 自动补 finish 的历史形状)与 `"strict"`(计划请求按普通脚本步回放,契约由它来证);未知取值报错 |

`completion="text"` 一路**不计算 `patch_applied`**:计划段没有写工具,"没有补丁"是构造事实而不是
观测,而 `working_tree_diff` 在非 git 工作区会抛 `GitCmdError`(M10 的教训)——合规回复不该被现场条件炸成阶段异常。

### 最小行为证据(输入 → 实际请求/动作 → 结果)

离线探针(零网络、零模型),`docs/evidence/agent-upgrade/probe-after-g01.txt`
(sha256 前 16 位 `54c134ba9062f378`):

| 读数 | 修复前(`probe-before-g01.json` `dd076327965192ca`) | 修复后 |
| --- | --- | --- |
| 连续三条合规纯文本计划的 PLAN 段 | `calls=3`,`error="agent loop exceeded max_turns=3"` | `calls=1`,`completion="text"`,summary 60 字符 |
| 计划段模型实际收到的 schema | 10 个全量 | `["finish"]` 一个 |
| 计划段要 `read_file` | 先执行/被挡再重想,一路烧到份额 | `calls=2`,`tool_executed=false`,落 `protocol_repair` 后第二段文本收束 |
| 对照臂(同一条行为、不开契约) | — | 仍 `calls=3` + `max_turns` 异常 ⇒ 默认形状未变 |

用例面:`tests/test_tool_capabilities.py` 18 条(新增,sha256 `7f34bb57145dcad2`)+
`tests/test_llm.py` 1 条(空能力集合省略 `tools` 字段、非空时原样转发不加回全量)。
覆盖 SPEC 验收矩阵 T01(合规文本一次完成)、T02(截断/隐藏工具有界结束且零非法副作用),
另加"伪造未注册工具执行不了"、"LOCALIZE 看不到写/测工具"、"两臂 schema == 登记面"、
"白名单名字必须已注册"、"legacy 文本+finish 与新文本等效收束"。

### 检查读数

```
.venv/Scripts/ruff.exe check .            → All checks passed!
.venv/Scripts/ruff.exe format --check .   → 235 files already formatted
定向:tests/test_plan_stage+test_plan_invariants+test_plain_loop+test_single_shot+test_llm → 59 passed
定向:tests/test_docs_anchors+test_graph+test_resume+test_resume_crash+test_experiment_summary+
      test_driver+test_report → 78 passed
定向:tests/test_tool_capabilities → 18 passed
全量:.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b0-full
      → **899 passed, 4 skipped, 4 warnings in 2240.96s (0:37:20)**,exit 0
```

全量数对账:基线 880 + 本卡新增 19(`test_tool_capabilities` 18 + `test_llm` 1)= 899,逐条闭合。
4 条 skip 与基线**同一组**(符号链接权限 ×2、Redis 不可用 ×2),没有新增 skip。
耗时 37:20 对基线 26:02:新增用例只多跑两次真 pytest(约 20s 量级),其余差值归因未查,
按机器负载记账,**不作为回归证据**——同类的 `duration_ms` 抖动在 M9.1 已经登记过。

旧断言的处理:一条都没删、一条没放宽。`test_plan_invariants` 的
`calls[1]["allowed_tools"] == [FINISH_TOOL]`、`test_plan_stage` 的 `consumed` 免步契约、
`test_graph` 的 "not allowed in phase" 拦截用例全部原样通过;
`tests/test_docs_anchors.py` 只改了 `nodes.py` 的行号索引(文档正文与代码注释移动所致,
期望子串一字未动),并按同一口径重钉 ADR-0004/0006/0007/0009 的锚点行号。

### 剩余限制(不许被读成"已证明效果")

1. 有界修正取 1 是按 Q0 的失败形状定的,**不是标定值**;真实模型能否稳定给出合规纯文本计划
   没有在线证据 —— 属 G17。
2. `plan_invalid` 降级交出去的是**最近**一段实质文本,可能没有计划价值;本卡只保证不再烧空份额。
3. 续跑时修正计数从 0 重计(A 级快照不存协议状态),最坏情况一段在多次恢复里各问一次修正。
   持久化要改快照形状,属 G05/G06。
4. 计划段仍暴露 `finish` 一个 schema(为了 legacy 等效路径)。真实模型多一个可用的收尾手段,
   这是刻意的兼容,不是漏网。
5. 离线回放绿只证明协议与权限接线,**不证明修复率**;resolved 依旧只由门禁 + 失败/回归双集决定。

### 费用与外部动作

零模型调用、零付费、零 push、零删除历史结果。全部证据可在本地重跑(命令见上)。

### 下一卡

B1(G02–G04:usage 来源、共享原子预算、比较器与预登记入口)。

---

## B1 说明

起点 HEAD `f40ae36`(B0 结束)、开工 dirty 0。三张卡的落地 commit:G02 `9661b3f`、
G03 `21bf1fd`、G04 `898d662`,批次文档与进度另发一个 commit。全量读数以「B1 批次退出」
一节为准:本批只在批尾跑一次全量,卡内跑定向集(同一仓库并起多个 pytest 会互相踩
basetemp 与工作区,这条纪律见 PROGRESS 的 M10/M11)。

---

## G02 / P1:模型响应与计量协议,异常先计量

- **开始身份** `f40ae36` → **结束身份** `9661b3f`。
- **范围**:`app/llm/`(新增 `usage.py`)、`app/errors.py`、`app/graph/plain_loop.py`、
  `app/tools/tracker.py`;用例 `tests/test_llm.py` + 新增 `tests/test_usage_envelope.py`。
  `app/graph/resources.py` 本卡没改:SPEC 给的只能是"接线",接线点在循环侧,账本自身的
  算法留给 G03。范围外扩张:无。

### 改了什么语义

| 文件 | 语义 |
| --- | --- |
| `app/llm/usage.py`(新) | `UsageEnvelope`(来源只有 provider/estimated/unknown;None ≠ 0;缓存明细永远是 prompt 的子集,入账总额不含它;未知字段带去敏副本)、`AttemptRecord`(每次物理尝试独立 ID/用量/状态 + `retry_owner` 与 SDK 上界)、`ModelResponseEnvelope`(`as_report_dict()` 无思维链原文)、`RequestEstimate`(把有效 schema、tool arguments、需回传的 reasoning 与协议常数算进"待发请求",方法与版本一起存) |
| `app/llm/openai_client.py` | 顺序改成"① 先解析 usage ② 再解释 choices/工具参数/finish_reason";空 choices、截断带工具调用、参数不可解析都显式归类并带 envelope 抛出;新增 `TransportRetryPolicy`(平台显式重试要求 `llm_max_retries=0`,两层同开在构造处拒绝);没有平台重试可报告时**原样上抛**原异常,类型与既有映射逐字不变 |
| `app/errors.py` | `ModelResponseError` / `ModelTransportError`(都带 `envelope`),均为 `TaskError` 子类 ⇒ 既有 `pytest.raises(TaskError)` 与 API 错误映射不受影响 |
| `app/graph/plain_loop.py` | `_usage_of()`:回合带 envelope 就用它,旧桩/回放按 `from_turn_ints` 如实降为 **estimated**(取消账本默认的 provider 标签);异常先计量再上抛;`llm` 轨迹事件新增 usage_source / finish_reason / request_kind(`normal` \| `protocol_repair`)/ physical_attempts / parse_failures / 估算版本;修正请求、thinking、length 截断都显式归类 |
| `app/tools/tracker.py` | 事件落盘按键名去敏(`reasoning_content`/`api_key`/`authorization`/`x-api-key`)——轨迹经 `GET /tasks/{id}/trajectory` 原样对外(T24) |

### 最小行为证据(全部离线,零网络)

| 场景(输入) | 预期 | 实测 |
| --- | --- | --- |
| usage=321 + `choices: []`(T03) | 已知消耗随异常交付 | `envelope.usage.chargeable_total==321`、`parse_failures==["choices_missing"]`、attempt 状态 `parse_failed`;循环侧先入账:`ledger.tokens_used==321`、call 状态 `recorded`、落 `model_response_invalid` 事件 |
| 同形状但没有任何 usage 可带(连接都没建立) | 仍 `unknown`,不写 0 | `record.status=="unknown"`、`tokens_used==0` |
| DeepSeek 命中/未命中 600/200,total 1000 | 回合读数仍是 1000 | `cached_tokens==600`、`uncached_input_tokens==200`、`chargeable_total==1000` |
| 端点不返回 usage | 来源 estimated + 写明原因 | `usage_source=="estimated"`、`estimate_reason=="endpoint_omitted_usage"` |
| `llm_max_retries=3` | 平台看不见次数 | `retry_owner=="sdk_opaque"`、`sdk_retry_bound==3` |
| `llm_max_retries=0` + 显式重试 1 次,首次连接失败 | 两次尝试、ID 不同、失败那次 unknown | `attempts==[transport_failed, ok]`、`chargeable_total==50`、`unknown_attempts==1` |
| 400 内容错误 | 不重发(重发只是再花一遍) | `calls==1`、`unknown_attempts==1` |
| 截断 + 工具调用 | 补丁不执行且花费可查 | 抛 `ModelResponseError`(message 仍含 `truncated`)、`truncated=True`、`chargeable_total==99` |
| 思维链端点 | 公开读数不含原文 | `as_report_dict()` 中不含"先想一下失败原因" |
| 协议修正那一次请求 | 能单独数出来 | `llm` 事件 `request_kind==["normal","protocol_repair"]` |

用例面:`tests/test_usage_envelope.py` 31 条(sha256 前 16 `d6275f855867a966`),
`tests/test_llm.py` 新增 13 条(共 30 条);覆盖验收矩阵 T03、T04 与 T24 的公开面。

### 检查读数(卡内定向)

```
.venv/Scripts/ruff.exe check .            → All checks passed!
.venv/Scripts/ruff.exe format --check .   → 237 files already formatted
定向:tests/test_usage_envelope.py + tests/test_llm.py                    → 61 passed
定向:tests/test_llm.py + test_p2_cleanup.py + test_plain_loop.py         → 46 passed
定向:tests/test_plain_loop.py + test_f1_budget + test_resources +
      test_tool_capabilities + test_plan_stage + test_plan_invariants +
      test_p2_cleanup + test_docs_anchors + test_trajectory_summary +
      test_live_progress                                                  → 92 passed
```

存量红:本轮无产品失败。行锚漂移(`plain_loop.py`/`nodes.py`/`runner.py`/`config.py`/
`resources.py`)在同卡内按实测行号重钉,被钉的期望子串一字未动。

### 兼容与剩余限制

1. 默认形状逐字不变:`complete_on_text` 默认 False、`TransportRetryPolicy` 默认 0 次额外尝试、
   SDK 上界仍按 Settings 透传;`llm_max_retries>0` 时平台绝不自己重试;
2. `TransportRetryPolicy` 目前**没有生产调用方** —— 装配点是 G07(`app/llm/profiles.py`)的
   声明范围。机制与用例先落地,这是范围决定不是遗漏;
3. 请求估算 `RequestEstimate` 不进额度闸:`pending` 那个读数被 M15/M17 的历史锚点钉住
   (`docs/evidence/2026-10-08-context-corpus-calibration.txt`),改它就是回溯改口径。
   它的消费方是 G03 的预留与报告;
4. 台账仍不能表达"一次调用里部分已知 + 部分未知":未知笔数在轨迹与 envelope 里,
   账本侧的未知持有(G03)只按预留量表达;
5. 各端点 cache 字段的真实计费口径、"重试是否真的少花钱"都没有在线证据(G17)。

### 费用与外部动作

零模型调用、零付费、零网络、零 push、零删除历史结果。

---

## G03 / P1:共享原子预留、容量与费用账本

- **开始身份** `9661b3f` → **结束身份** `21bf1fd`。
- **范围**:`app/graph/resources.py`(增量扩展,不另建第二套计数器)、`plain_loop.py`、`nodes.py`、
  `runner.py`、`app/evals/driver.py`、`pricing.py`、`app/api/report.py`、`config.py`、
  `task_spec.py`;用例新增 `tests/test_budget_reservations.py` 26 条
  (sha256 `9ba9be0c32e1fdb9`),`test_resources.py` / `test_f1_budget.py` / `test_time_budget.py`
  原样通过。`app/graph/loop_state.py` 与 `resume.py` **未改**:账本快照走自己的原子文件
  `run_dir/ledger_state.json`(SPEC §4 允许的"现有原子文件快照"),恢复的读取点收在
  `ledger_from_settings()` 里,不侵入阶段快照的语义。
- 范围外扩张:`tests/test_resume.py` 改了一条用例的**崩溃注入点**(断言一字未动),
  经用户 2026-10-11 裁决,理由见"需要点名的一处语义变更"。

### 改了什么语义

| 项 | 语义 |
| --- | --- |
| 原子预留 | `reserve(call_id, stage, request_tokens)` 在**同一个锁**里完成检查与登记,比较基数是"已用 + 全部在途预留 + 未知持有 + 本次预留"。旧形状只看已用 ⇒ 两个各自装得下的并发请求都放行(T05) |
| 未知消耗 | `abandon_call` 把预留转成 `unknown_held_tokens/_cost` 并**继续占额度**;随快照落盘、恢复读回。旧形状:恢复后账本全新 = 把已花的额度还回来 |
| 结算幂等 | `record_usage` 关闭对应预留;同 call_id 第二次入账无效且记 `duplicate_settle_ignored` 异常 |
| 维度补齐 | `budget_mode=tokens\|cost\|hybrid`(默认 tokens ⇒ 既有行为逐字保留)、`max_model_calls`、`model_context_tokens + capacity_margin_tokens`(越过声明容量在请求前拒绝并计数)、`cost_budget_micro_usd` |
| 费用 | micro-USD 整数累计;`PriceCard` 分档(未缓存输入 / 缓存读 / 缓存写 / 输出),端点未声明缓存价时那部分按未缓存价全额计(宁可偏贵,不虚构折扣);每笔带 `basis`(local_estimate / provider_usage_priced / settled_bill)+ `price_version` + `price_source` + `currency`;`settled_bill` 只能外部显式入账——"端点 usage 乘价表"永远是估计账单 |
| 可重算 | `recomputed()` / `is_consistent()`:累计量必须等于调用明细之和;报告带 `calls_consistent` |
| 受理 | `validate_budget_policy()`:在线 cost/hybrid 缺金额上限或缺价目 ⇒ 拒绝(一次请求都不发);离线/fake 不受此约束(否则回放与既有批次全停摆) |
| 取消 | 回复与取消同时到达:先结算这一次花费,再补齐 tool 配对消息、落快照、抛 `TaskCancelled`——工具一个都不执行,而取消点仍是可续位置 |

### 最小行为证据

| 场景 | 实测 |
| --- | --- |
| 两个线程同时 reserve 600/上限 1000(T05) | 恰好一个 `granted`、一个 `rejected`(错误信息含 `in flight`);`committed_tokens==600`、`tokens_used==0` |
| 在途请求崩掉 | `unknown_held_tokens==700`、`committed_tokens==700`,随后 400 的预留被拒 |
| 恢复读回快照 | `ledger_from_settings()` 重建:`tokens_used / unknown_held / resource_status / limits` 全部沿用;同 run_dir 二次构造不再归零 |
| 坏快照 | 读不动 ⇒ 回落新账本并告警,不把"读不到"读成"没花过" |
| 现金超支 | 预留 2.5e6 通过、实际回复 3.5e6 ⇒ `cost_overrun` 记录、`resource_status==exceeded` |
| cost 模式无价目 | `reserve` 直接拒绝(`requires a known price`),不"免检放行" |
| 调用数上限 2 | 第 3 次调用被拒(`model-call limit`),与 token 数无关 |
| 声明容量 8000 / 余量 1000 | 8500 的请求发出前被拒,`over_capacity_requests==1`,总额状态仍是 within_budget(这是容量不是额度) |
| hybrid | cost 模式下旧 token 闸照样拦(`task token budget … exhausted`),不静默取消 |
| 明细重算 | 三次入账后 `is_consistent() is True`、`recomputed()["tokens_used"]==ledger.tokens_used`、stage_usage 逐项对上 |
| 崩溃在请求中途 → 恢复 | 续跑终局 `BUDGET_EXCEEDED`,`resource_status==unknown`,`unknown_held_tokens` 与崩溃时一致,verdict 不是 resolved(§3.1) |

### 检查读数(卡内定向)

```
定向:tests/test_budget_reservations.py                                   → 26 passed
定向:tests/test_resources + test_f1_budget + test_pricing + test_acceptance
      + test_usage_envelope + test_time_budget                           → 74 passed
定向:tests/test_graph + test_resume + test_resume_crash + test_api +
      test_report + test_single_shot + test_plan_stage + test_plain_loop +
      test_cancellation                                                  → 105 passed (0:05:22)
```

旧断言处理:一条没删、一条没放宽。`test_resources.py` 13 条、`test_f1_budget.py` 7 条、
`test_acceptance.py` 的"state/ledger 取大"防线全部原样通过;
`test_abandoned_pending_call_marks_unknown_without_zeroing` 依赖的 `"without usage"` 文案保留
(只在其后追加持有量);`test_resources.py::test_ensure_request_fits_...` 的
`exhausted` 语义与"已用不清零"一字未动。

### 需要点名的一处语义变更(经用户裁决)

**恢复遇到 unknown 不再能判 resolved。** 旧用例依赖的行为正是本卡要堵的洞:崩溃时那次
在途请求的花费无从知道,而 SPEC §3.1 要求"资源 unknown 保持严格拒绝/待审"、G03 要求
"恢复不能释放未知余量"——恢复把账本擦新等于从终态上绕过了它。
处置(用户选"改崩溃注入点 + 另加新用例"):

- `tests/test_resume.py::test_graph_resume_finishes_and_reruns_every_gate`:**断言一字未动**,
  只把崩溃点从"第 5 次请求的 complete() 中途"移到"第 2 轮回复已结算、run_tests 未执行"
  (账本没有未定在途),继续证明"复位 + 门禁重跑 + resolved";
- 新增 `tests/test_budget_reservations.py::test_midrequest_crash_resume_cannot_certify`
  钉住新语义;
- 因此失去使用者的 `_CrashAfter` 辅助类已删(不留无使用者的测试脚手架)。

### 剩余限制

1. 预留量是估算(`chars/4` + 协议常数),真实 tokenizer 下的误差未标定(G17);
2. 现金闸仍可能被单次在途请求越过(§5 明说现金预算不保证 provider 绝不超支);
   越过后如实记 `cost_overrun` 并停后续动作,不追平到上限伪装合规;
3. `ledger_state.json` 只有 `LEDGER_STATE_VERSION=1`,版本不符即当没有快照(保守冷启),
   不做"猜着升版"的迁移;
4. 旧 run_dir(崩溃在那次落盘之前)恢复行为与今天逐字相同:账本仍是新的——
   这条路径的存在由 `load_ledger_state` 返回 None 明确表达,不假装覆盖;
5. 报告新增 `resources` 字典是增量字段,旧报告缺键时 API 显示"未记录"而非"合规";
   历史批次的 `cost_usd` float 约值口径保留,与新 `cost_micro_usd` 并存不互相换算。

### 费用与外部动作

零模型调用、零付费、零网络、零 push、零删除历史结果。

---

## G04 / P1:provenance、比较器与预登记入口

- **开始身份** `21bf1fd` → **结束身份** `898d662`。
- **范围**:`app/evals/provenance.py`、`app/evals/metrics.py`(只扩身份与统计,**不碰 resolved
  判定逻辑**)、新增 `app/evals/experiment_manifest.py`(实验辅助模块,按 SPEC §4 落在
  `app/evals/`)、`scripts/compare_experiments.py`、`scripts/run_paid_batch.py`、
  `scripts/summarize_paid_batch.py`、`app/graph/runner.py`(把策略对象交给 provenance 登记);
  用例新增 `tests/test_experiment_manifest.py` 30 条(sha256 `29d6ad3b8b93b6a7`)、
  `tests/test_experiment_summary.py` +8 条、`tests/test_driver.py` 的 SNAPSHOT_KEYS pinned 集补键;
  新实验模板 `docs/work-notes/experiment-manifest-template-2026-10-11.json`
  (sha256 `c571063d00245848`)、新 ADR `docs/adr/0011-实验登记面-manifest-是唯一批定义.md`
  (sha256 `3cdaa65082f9bc3d`)。范围外扩张:无(`app/task_spec.py` 本卡未再动)。

### 改了什么语义

1. **结果不是控制变量**。`OBSERVED_FIELDS`(verdict/status/tokens/rounds/duration/费用/门禁/
   changed_files/error/资源摘要…)允许不同,永不进入差异判定;`CONTROL_SECTIONS`
   (git commit、脏树指纹、config_snapshot、端点/价目/容器/协议/预算身份、任务与源码与
   测试清单身份、`provenance_schema_version`)必须逐字段相同,缺节读成 `<absent>` 而非"相同"。
   只有 manifest 登记的变量(按 `PATCHPILOT_*` → `config_snapshot.<settings 键>` 映射)
   与臂派生字段(`arm`/`experiment_policy`/策略 hash)可以不同;
2. **每次重复都保留**。`pair_runs(rows, arms=…, expected_repetitions=…)` 按
   `(task_identity, arm, repetition)` 配对,一条不折;缺对、重复 task_id、次数与 manifest
   不符各自单独诊断。`latest_per_bug` 保留作历史汇总口径,比较器不再经过它;
3. **登记面补齐**:`model_identity`(显式 `alias_proves_served_version=false`、
   `served_model=null`)、`endpoint_identity`(规范化 host+path + URL 短 hash,
   userinfo/query/凭据一律不落盘)、`protocol_identity`(工具 schema 指纹、提示词指纹、
   策略描述与 hash、预算模式、输出上限、裁剪行数)、`pricing_identity`
   (`PRICING_VERSION` + 覆盖文件内容 hash,不写路径)、`container_identity`(只有 docker 才查
   本地 digest;查不到写 `tag_only`/`unavailable`,local 后端写 `not_applicable`)、
   `budget_identity`;`PROVENANCE_SCHEMA_VERSION=2`,跨结构版本互比直接拒绝;
4. **Settings 键重分类**:`llm_max_tokens`、`llm_max_retries`、`llm_thinking`、`max_read_lines`、
   `max_search_results`、`max_output_chars`、`refine_head_lines`、`refine_tail_lines`、
   `docker_image` 从 EXEMPT 移入 SNAPSHOT(SPEC 原文:"文本裁剪/输出上限不是复现豁免")。
   `price_overrides` 与 `llm_base_url` 仍留 EXEMPT(值是本地路径/可能内嵌凭据),
   但它们的**身份**由上面两节以 hash 登记;
5. **paid driver 只认 manifest**。题单/臂与登记变量/重复数/额度/停手条件/授权/产物目录
   全来自那一份可 hash 文件;`--dry-run` 只展开矩阵与额度包(零子进程、零网络、不建锁、
   不写台账);`--phase` 起真实运行一律拒绝,Q0 常量留在文件里作**只读历史记录**;
   台账每条带 `manifest_hash` 与资源摘要;`summarize_paid_batch.py` 新增按 manifest 聚合的
   观测分布视图,Q0 判据块只服务历史 phase,新批不再隐式继承它。

### 最小行为证据

| 场景 | 实测 |
| --- | --- |
| T07:同配置、结果不同、各 3 次重复 | 比较通过;`runs_kept==6`,verdicts 逐运行列出,`repetitions=={agent:3, one_shot:3}` |
| T08:结果全同、`context_window_tokens` 漂移 | 拒绝:"未登记的配置漂移:`['context_window_tokens']`" |
| thinking 档位漂移 | 拒绝(错误信息含 `llm_thinking`) |
| manifest 登记 `PATCHPILOT_CONTEXT_WINDOW_TOKENS=0` | 接受,摘要带 `manifest_hash` |
| 同 (bug, arm) 出现两个相同 task_id | 拒绝:`duplicate run id` |
| manifest 说 3 次、实际 2 次 | 拒绝:`repetition mismatch`(不补跑也不剔除) |
| 旧批(无 `provenance_schema_version`)vs 新批 | 拒绝:`provenance_schema_version differs: '<absent>' != 2` |
| URL 含 `?key=…` 与 userinfo | 整个 provenance JSON 里既无 `sk-live-nope` 也无 `bearer-secret`;host 只剩 `api.example.com` |
| 在线 manifest 缺授权 / 缺金额上限 | 校验拒绝(2 条用例);`--phase` 旧命令退出码 1 |
| `--dry-run`(`subprocess.run` 换成"一调就炸"的桩) | 返回 0;锁文件与台账都没被创建;`network_requests==0` |

### 检查读数(卡内定向)

```
定向:tests/test_experiment_manifest.py                                   → 30 passed
定向:tests/test_experiment_summary + test_metrics + test_provenance_anchor
      + test_bugset + test_report + test_experiment_manifest              → 165 passed (0:02:17)
定向:tests/test_docs_anchors.py(含 ADR-0011 的 8 条新锚点)              → 6 passed
```

旧断言处理:`test_registered_arm_difference_is_the_only_allowed_one` 里**读摘要形状的两行**
改成新形状(`["resolved"]` 而非 `"resolved"`),断言强度没降(仍然断言两臂各自的 verdict 与
token,并新增 `runs_kept`/`pairing` 两条断言);其余 3 条比较器用例一字未动并通过。
`test_driver.py` 的 SNAPSHOT_KEYS pinned 集按该用例 docstring 自己规定的维护动作补键
(新增分类即要补,一个键也没删)。

### 剩余限制

1. `repetition` 由配对结果推导(task_id 排序编号),运行产物里没有 rep 字段;要让
   manifest 的 rep 与运行一一对上,需要 TaskSpec/RuntimePolicy 的接线点(G07/G16);
2. `served_model` 没有采集(客户端未读响应里的 model 字段)——契约 1 的 ModelProfile 属 G07,
   这里如实记 `null` 并留 `alias_proves_served_version=false`,不假装证明过;
3. 跨 `provenance_schema_version` 的旧批只能**拒比**,不做"降级比较";旧批次报告与 Q0 台账
   一字未改,只读保留;
4. `container_identity` 依赖本机 Docker daemon:同一 manifest 在两台机器上可能一个是
   `checked` 一个是 `tag_only`,这会被比较器报成未登记差异——按事实拒比,不加豁免;
5. 摘要与 ADR 都不产出"修复率/净增益"表述;比较器只能证明"控制身份相同",
   不能证明结果归因于登记变量(ADR-0011 的反方节)。

### 费用与外部动作

零模型调用、零付费、零网络、零 push、零删除历史结果。dry-run 用例把 `subprocess.run`
换成"被调用即抛"的桩,证明这条路径不发起任何进程。

### 下一卡

B2(G05–G08:输出可取回、证据跨阶段、容量 profile、仓库规则)。需用户点头后再起;
G17(真实端点探针与在线小批)另需逐批授权,本批没有跑任何真实调用。

---

## B1 批次退出

```
.venv/Scripts/ruff.exe check .            → All checks passed!
.venv/Scripts/ruff.exe format --check .   → 241 files already formatted
全量:.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b1-full
      → 1007 passed, 4 skipped, 4 warnings in 1569.64s (0:26:09),exit 0
```

数对账:B0 结束的全量是 `899 passed, 4 skipped`;B1 新增用例 G02
`test_usage_envelope` 31 + `test_llm` +13、G03 `test_budget_reservations` 26、
G04 `test_experiment_manifest` 30 + `test_experiment_summary` +8 ⇒ **+108**,
实得 `1007 passed` 与 899+108 逐条闭合。skip 是**同一组 4 条**(`test_input_snapshot.py:67`
与 `test_search_tools.py:746` 的符号链接权限 ×2、`test_locks.py:49/68` 的 Redis 不可用 ×2),
新增 skip 数为 0,也没有一条为消掉失败而加的 skip。
warnings 同为 4 条,类别与基线一致(starlette DeprecationWarning、`TestRunResult` 收集告警、
语料回放里的 SyntaxWarning、`test_demo_smoke` 子进程读取线程的 GBK 解码告警)——
**没有新增告警类**。耗时 26:09 对 B0 的 37:20:本机负载差异,不作为回归或改进的证据。

日志:`D:/tmp/pt-upgrade-b1-full.log`(控制台按 GBK 码页输出,读数由脚本按 gbk+replace 解码取,
本机已知限制)。

### 一处流程违规,照实记

批次全量在跑期间,我又起了一次 `tests/test_docs_anchors.py`(basetemp 用的是另一个目录
`D:/tmp/pt-upgrade-b1-check`)。这违反本仓库"全量在跑时不起任何 pytest"的纪律
(见本文件与 AGENTS 里 M10/M11 的成因)。后果评估:那次运行只读文档与源码行,不建工作区,
且终态数与预期精确闭合,没有可疑失败需要归因;但纪律就是纪律——**下一次批尾全量启动后,
在任何结果落地之前不再起任何 pytest**,需要复核就等它跑完。

---

## B2 批次说明(G05–G08)

用户 2026-10-11 点头起 B2。批次目标(SPEC §6):**输出可取回、证据跨阶段、容量 profile、仓库规则**。
依赖链 G03+G04→G05→G06→G07→G08 决定了卡序不许打乱;每卡一个代码 commit + 一次进度登记。

B2 全程零模型调用、零付费、零 push、零删除历史结果。

---

## G05 / P1:结构化输出、证据存储与分页取回

- **开始身份** `72e169e` → **代码结束身份** `cdd7cdc`(本进度与 PROGRESS 在紧随其后的 docs commit)。
- **允许的语义变更**:SPEC §3.2 明列的"能力对应 schema / 结构化输出";交接提示已授权在
  `app/graph` 等必须掌握区按规格实现协议语义,不再就同一件事重复请示。
- **声明范围内**:`app/tools/output_filter.py`、`app/tools/base.py`、`app/tools/files.py`、
  `app/tools/registry.py`、`app/tools/tracker.py`(未改,去敏面 G02 已就位)、
  `app/context/evidence.py`(新增)、输出/检索测试与新增 `tests/test_evidence_store.py`。
- **范围扩张(动手前先登记,逐条给理由)**:
  | 文件 | 为什么必须动 |
    |---|---|
  | `app/graph/nodes.py` | 新工具不进 `READ_TOOLS`/`WRITE_TOOLS` 就是"模型看不见、调了才被挡"——G01 立的正是"可见面==执行面";只加两行名单 |
  | `app/evals/single_shot.py` | `ONE_SHOT_TOOLS` 同步加,两臂对称(私给一臂少一个能力=未登记 handicap,这条在 `swe-hard-oneshot` 批上用 777k token 买过教训) |
  | `app/tools/patching.py` | 撤 `git_diff` 自带的 `[:20_000] + "(truncated)"`:SPEC 原文"不把每个工具各做一套截断器",而它切掉的 diff 尾巴既没落盘也不可取回 |
  | `app/config.py` | 5 个新键必须走 Settings(AGENTS:禁止硬编码路径与上限) |
  | `app/evals/provenance.py` + `tests/test_driver.py` | 新键的三选一归类是既有强制机制(`test_settings_keys_fully_classified_for_snapshot`),pinned 集按该用例 docstring 规定的维护动作补键,一个键也没删 |
  | `README.md` + `tests/test_docs_anchors.py` | 工具清单条数与名单以注册表为准绳(`test_readme_tool_inventory_matches_registry`);nodes.py 的行索引 697→699 |
  | `docs/adr/0003/0004/0005/0006/0007/0009/0011` | 7 篇 ADR 的验证锚点行号重钉(共 11 处,`plain_loop/nodes/registry/files/config` 行漂移) |
  | `scripts/probe_g05_bounded_view.py` | SPEC §9 要求"最小行为证据…工件路径/hash"可本地重跑,收据必须由脚本再生而不是手抄 |

### 改了什么语义

| 文件 | 语义 |
|---|---|
| `app/context/evidence.py`(新) | `EvidenceRef`(任务内 id、来源、kind、`workspace_revision`、内容 sha256、字节/行、`retrievable`+原因)、`EvidenceStore`(原子落盘 + `index.jsonl`;单工件与总量两级上限,超了就**拒落并带着身份抛** `EvidenceNotStored`;`read()` 按字节偏移分页、预算内才对齐行尾、hash 不符即拒读、跨任务读成 `not registered in this task`)、游标 `"<偏移>:<hash 前 12 位>"`(把游标钉死在工件上) |
| `app/tools/output_filter.py` | 新增 `bound_structure`/`fold_text`/`BoundReport`:**在数据结构上裁**——首轮按 `refine_head/tail_lines` 的形状折长字段,装不下才按字符预算退让,仍超限才开始丢列表尾巴;每处省略带计数,`line_ranges` 给出**保留的原始行号区间**。`fold_output`/`refine_traceback` 原样保留(历史回放与 traceback 提纯仍用) |
| `app/tools/registry.py` | `execute()` 统一过 `_apply_visible_view`:裁剪 → 原文落盘 → 组装模型可见 payload → 补一条 `evidence_bounded` 取证事件(原文/excerpt 各自 sha256、字节数、可取回标志)。`MUTATING_TOOLS` 在 `apply_patch`/`reset_workspace` 成功时给 `ctx.workspace_revision` +1(写代数只在这一处定义,门禁拒绝不留代数);新注册 `read_evidence`;`model_payload()` 是"发给模型那一份"的唯一入口 |
| `app/graph/plain_loop.py` | 工具消息组装改走 `model_payload(result)`,**不再** `json.dumps` 之后 `fold_output`(旧路径切在 JSON 中间);错误回执走同一条有界协议 |
| `app/tools/files.py` | `read_evidence(evidence_id, cursor, limit_bytes)`:仓未建也照常查(答案统一是"本任务没登记这个 id",不给"你的任务是空的"这种存在性旁证);`stale=true` 时明说"不能当现状用" |
| `app/config.py` | `tool_visible_max_chars=8000`(与旧 hard_cap 实际生效值一致,不收紧不放宽)、`evidence_enabled=True`、`evidence_max_item_bytes=2_000_000`、`evidence_max_total_bytes=64_000_000`、`evidence_page_bytes=4000` |

### 最小行为证据(输入 → 实际请求/动作 → 结果)

真实素材收据(零模型、零网络,`.venv/Scripts/python.exe scripts/probe_g05_bounded_view.py`),
`docs/evidence/agent-upgrade/probe-after-g05.json` sha256 前 16 位 `bf63af592b2d64f6`:

| 真实 `read_file` 回执(缓存的上游仓库) | 原文字符 | 旧路径 | 新路径 |
|---|---|---|---|
| sphinx-75900 `sphinx/util/cfamily.py` | 13,425 | 8,013 字符,`json.loads` **失败**,5,410 字符无痕迹消失 | 2,457 字符,合法,省略 345 行,原文逐字节可取回 |
| astropy-87070 `astropy/io/fits/card.py` | 16,473 | 8,013 字符,非法,8,458 字符丢失 | 2,174 字符,合法,省略 344 行,可取回 |
| pylint-69030 `pylint/lint/run.py` | 8,690 | 8,013 字符,非法,675 字符丢失 | 2,612 字符,合法,省略 177 行,可取回 |
| requests-11420 `requests/adapters.py` | 7,331 | 未超限 ⇒ 原样 | 未超限 ⇒ **一字不变**(不加任何键) |

3 份超上限回执:旧路径 **3/3 生成非法 JSON**;新路径 **3/3 合法且原文分页拼回逐字节相等**
(`raw_reassembled_all=true`)。用例面 23 条(`tests/test_evidence_store.py`,含验收矩阵
T09 端到端与一条**反向对照**:同一 payload 走旧 `fold_output` 必然 `JSONDecodeError`)。
另含:未知/他任务/伪造路径 id 一律 `not registered`;游标格式错、偏移越界、A 游标用到 B 上
各自单独报错;篡改工件 → `hash mismatch`;新进程只靠索引续读;单工件/总量两级上限拒落且
**明说** `retrievable=false`;`evidence_enabled=false` → `raw_saved=false` + 原因;
写后旧引用 `stale=true` 且内容仍是改前那份;嵌套 traceback 列表被逐字段折。

### 检查读数

```
.venv/Scripts/ruff.exe check .            → All checks passed!
.venv/Scripts/ruff.exe format --check .   → 245 files already formatted
定向:tests/test_evidence_store.py + test_output_filter + test_docs_anchors → 44 passed
大范围:32 个受影响测试文件(G05 语义面)→ 479 passed / 1 skipped / 1 failed(那 1 条是我自己
      新写的 ADR-0012 锚点行号,重钉后转绿)
全量:.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b2-full
      → 1030 passed, 4 skipped, 4 warnings in 2314.57s (0:38:34),EXIT=0
```

数对账:B1 批次退出是 `1007 passed / 4 skipped`;G05 新增 23 条(`--collect-only` 实测 23 collected)
⇒ `1007 + 23 = 1030`,逐条闭合。skip 是**同一组 4 条**(`test_input_snapshot.py:67`、
`test_search_tools.py:746` 符号链接权限;`test_locks.py:49/68` Redis 不可用),新增 skip 为 0。
warnings 同为 4 类,与 B1 一致。日志 `D:/tmp/pt-upgrade-b2-full.log`(本机 GBK 码页,
读数按 gbk+replace 解码取——仓库已知限制)。耗时 38:34 对 B1 的 26:09 属本机负载差异,
**不作为回归或改进的证据**。

旧断言处理:一条没删、一条没放宽。`test_search_tools.py:764`(read_file 键集合)、
`test_tools.py:113/116`(search 命中项键集合)、`test_trajectory_summary.py:59`(diff 摘要替换)、
`test_resources`/`test_f1_budget`/`test_acceptance` 全数原样通过;`test_plan_invariants.py:186`、
`test_repo_map.py:275`、`test_single_shot.py:146/149` 比的都是导入常量,名单变化自动跟随;
`test_driver.py` 的 SNAPSHOT pinned 集与 `test_docs_anchors.py` 的行索引按各自 docstring
规定的维护动作补/改(补 5 键、697→699,没有删任何东西)。

### 需要点名的几处判断(不是顺手做的)

1. **可见形状改由 `refine_head_lines/tail_lines` 决定,不再由 cap 的字符数决定**:一屏从 8,000 字符
   降到 2,174–2,612 字符。旧路径那 8,000 字符是"读不懂的一行",新的是"55 行读得懂 + 原文可取回"
   ——这是**形状变化,不是能力提升**。G07 抬软窗口时必须把这两个键一起标定,否则窗口变大而每条
   回执仍是 55 行(已写进 ADR-0012 的反方节)。
2. **只在真裁过时改形状**:装得下的回执一个键都不加。否则每条存量断言与两臂对照解析都要跟着动。
3. **落点用 `report_dir.parent/evidence`**:与 `ledger_state.json` 同一个 run_dir 口径(nodes.py 现状),
   不新增第二个"产物根"概念。副作用是分支候选(`report_dir=<run>/reports/cand{i}`)与主流程
   **不共享索引**,读别人的 id 得到 not found——任务内作用域的直接后果,跨阶段共享域留给 G06 定。
4. **主工具轨迹事件仍记未裁摘要**,另加一条 `evidence_bounded`。两处形状不同是有意的:取证要能
   还原"原文形状",而"模型当时看到的那一屏"由 excerpt hash 与 id 钉住。
5. **撤掉的只有 `git_diff` 那把私刀**,`local_runner._truncate`(取末 `max_output_chars=20_000`)
   一行没动——那是执行层的既有口径,改它属 G09 `CommandProfile`(见下"剩余限制")。

### 剩余限制(不许被读成"已证明效果")

1. 五个上限取值(8000 / 2 MiB / 64 MiB / 4000)是工程起点,未标定;`evidence_max_total_bytes`
   对超长任务够不够,只有真实批能回答;
2. **命令 stdout/stderr 的全量落盘不在本卡**:`local_runner` 仍只保留末尾 20,000 字符(取尾不取头,
   头部至今无处可取)。SPEC 把"命令输出进入 EvidenceStore"写在 G09,那里才有"一次执行的完整输出
   是工件"的契约。本卡只保证**工具回执**这一层不再无痕丢失;
3. `read_evidence` 取回的是**文本切片**,不重解析 JSON:单行超长(正是 JSON 回执的常态)会切在字符
   中间并写 `line_aligned=false`。要精确重取某文件的另外几十行,`read_file(offset)` 更省;
4. 证据仓没有 GC:满了只拒落新工件,不删旧工件(删证据等于丢取证);`index.jsonl` 的残行被跳过
   而不修复(原始文件仍留盘);
5. `scripts/context_replay.py` / `measure_context_counterfactual.py` 仍按 `fold_output` 重建**历史批**
   的消息——那是那批运行当时的真实形状,不能改;但**G05 之后的新批**要用 `bound_structure` 才同形,
   这条已登记、未动(属 G16 收口时的测量器同步);
6. 离线全绿只证明协议与形状:**不证明修复率、不证明更少重查、不证明省 token** —— 都要真实批(G17)。

### 费用与外部动作

零模型调用、零付费、零网络、零 push、零删除历史结果。探针脚本只读本地缓存仓库并把证据写到
系统临时目录(仓库外)。

### 下一卡

G06(连续会话与结构化任务记忆):`app/agent/session.py`、`app/context/task_memory.py`、
`app/context/assembler.py` + `tests/test_task_memory.py`。G05 的 `EvidenceRef` 是它的引用单位;
SPEC §5 的"先去重复/替换可取回的大输出,再整理 TaskMemory,再压缩旧回合"要求
`compact_messages` 的存根带上 `evidence_id`(现在不带)——这条会在 G06 里做,不在本卡顺手改。

---

## G06 / P1:连续会话与结构化任务记忆

- **开始身份** `6874d90`(G05 文档收口)→ **代码结束身份** `cd92f00`;本进度与 PROGRESS §21 在紧随其后的 docs commit。
- **允许的语义变更**:SPEC §3.2 明列的"任务内会话/记忆";交接提示已授权在 `app/graph` 必须掌握区按规格实现协议语义。
- **声明范围内**:`app/agent/session.py`(新增)、`app/context/task_memory.py`(新增)、
  `app/context/assembler.py`(新增)、`app/graph/plain_loop.py`、`nodes.py`、`loop_state.py`、
  `resume.py`、`app/evals/driver.py`、`app/tools/base.py`、`tests/test_task_memory.py`(新增)。
  `state.py`/`prompts.py` 本卡**未动**,理由见"需要点名的范围决定"。
- **范围扩张(动手前先登记)**:
  | 文件 | 为什么必须动 |
  |---|---|
  | `app/tools/registry.py` | `update_memory` 必须注册才能进阶段白名单(G01 的"可见面==执行面");观测登记也必须挂在唯一执行入口上,否则每个工具各写一套 |
  | `app/tools/memory.py`(新增) | 观测折行(`describe_receipt`)与模型提交通道是工具层语义;SPEC G06 只列了 `tools/base.py`,工具实现按目录约定落在 `app/tools/` |
  | `app/graph/nodes.py` | 阶段装配点、候选/判定事实登记、rollback 的代数推进都在节点里 |
  | `app/config.py` + `app/evals/provenance.py` + `tests/test_driver.py` | 2 个新键的三选一归类是既有强制机制;pinned 集按该用例 docstring 规定的维护动作补键 |
  | `app/evals/single_shot.py` | 两臂对称(见"需要点名的决定"第 3 条) |
  | `README.md` + `tests/test_docs_anchors.py` + `docs/adr/0003/0004/0006/0007/0009/0011/0012` | 工具清单条数与名单以注册表为准绳;nodes.py 行索引 700→849;18 处 ADR 锚点因行漂移重钉(加上本卡自己 ADR-0013 的 3 处二次重钉,合计 21 处) |

### 改了什么语义

| 文件 | 语义 |
|---|---|
| `app/context/task_memory.py`(新) | `TaskMemory`:观测/判定/假设/排除四区,**事实与推断分开**;`record_evidence` 由平台写;`model_update` 只接受 `hypothesis|supported|refuted` 且 `supported/refuted` 必须引用真实存在的观测号,`verified/confirmed/resolved` 点名拒绝;`mark_stale(from_revision)`;`render` 三区分节、倒序保留最新、裁掉必须报数;**没有任何观测/假设/计划时不出块**(开局与接入前逐字节相同);`memory_state.json` 原子落盘,`schema_version` 不符一律当没有 |
| `app/agent/session.py`(新) | `SessionState`:session_id / 协议版本 / 阶段序列 / 每阶段能力集合 / 记忆版本;`identity_hash()` 是恢复比对的身份;**配对规则唯一实现处** `orphan_tool_calls()` + `repair_orphans()`(补的是显式占位,不伪造成功) |
| `app/context/assembler.py`(新) | `ContextAssembler` 按 `(阶段, 轮次)` **幂等**装配:骨架在前、记忆块随后、各只出现一次;二次装配返回同一成品并标 `reused=True` |
| `app/tools/memory.py`(新) | `describe_receipt`:把每条真实回执折成一行可核对事实(含行范围、是否被裁、遍历域是否被裁、run_tests 的通过/失败计数与失败用例);失败回执同样是观测。`update_memory` 工具的 handler |
| `app/tools/registry.py` | `execute()` 在 G05 的有界视图之后调 `_record_memory`(自动登记 + 写后失效);注册 `update_memory`;`MUTATING_TOOLS` 的代数推进与观测代数一致 |
| `app/graph/plain_loop.py` | 每次**发送前**过一道孤儿补齐并落 `message_pairing_repaired`;`_write_snapshot` 绑定 session/memory 四个字段并在同一边界落 `memory_state.json` |
| `app/graph/loop_state.py` | `LoopSnapshot` 新增 `session_id/session_hash/memory_version/memory_hash`(全部可空)。**不升 SNAPSHOT_VERSION**:字段是增量的,旧文件读回落在"未绑定",语义与加字段前逐字相同 |
| `app/graph/resume.py` | `binding_mismatch()` 纯函数 + 在 `prepare_resume` 里接:两侧都有绑定且不一致 → `reject`(待审),并落 `resume_binding_mismatch` 事件带两侧读数 |
| `app/graph/nodes.py` | `TaskNodes` 新增 session/memory/assembler 三字段(`task_memory_enabled=false` 时都不建);`_begin_assembly` / `_persistent_context`(改为"装配好就返回装配,否则返回骨架")/ `_open_agent_state`(冻结目标、约束、验收清单 + `session_opened` 事件);plan 产物、候选、verify 判定进记忆;rollback 推进代数并标失效 |
| `app/evals/driver.py` | plain 引擎同样接会话与记忆(与 graph 的 `_build_ctx` 同一口径),恢复优先读回 run_dir 那一份 |

### 最小行为证据(输入 → 实际请求/动作 → 结果)

用例 31 条(`tests/test_task_memory.py`),关键五条:

| 场景 | 实测 |
|---|---|
| T10 端到端:定位读过 `src/huge.py` 1-400 行(该回执超可见上限被裁)后进入 PLAN | PLAN 那一次**实际请求**的 system 里有 `[E1] LOCALIZE r0 read_file … 行 1-…` 与 `read_evidence("…")`;断言对象是 `model.seen[0]`,不是内部字段 |
| 模型试图把假设写成事实 | `status="verified"` → `rejected[0]` 点名 `platform-only`,该条**不落库**;无引用的 `supported` 同样被拒 |
| 崩溃切在"assistant 已声明、回执未落"之间 | 续跑的第一次请求里带着 `tool_call_id=orphan-1` 的显式占位回执,并落 `message_pairing_repaired`;旧路径会把孤儿原样发给端点 |
| 快照绑定 | `load_loop_snapshot` 读回的 `session_id/session_hash/memory_hash/memory_version` 与当前会话/记忆逐项相等;`binding_mismatch` 在"快照有绑定、记忆版本不同"时返回 `["memory_hash"]`,而**未绑定快照返回 `([], {})`** |
| 恢复重放同一份冻结上下文 | `set_context` 幂等:内容没变 ⇒ 版本与 hash 一字不动;真加了约束才 +1(这条就是第一轮两条 resume 红的根因) |

反向对照一条都没写进实现:装配幂等(`reused=True`)与"空记忆不出块"这两条本身就是防叠加/防行为漂移的钉子,
回归由 `test_repo_map.py::test_disabled_is_byte_identical_to_pre_change` 与
`test_assembly_is_stable_within_a_stage` 分别守着。

### 检查读数

```
.venv/Scripts/ruff.exe check .            → All checks passed!
.venv/Scripts/ruff.exe format --check .   → 252 files already formatted
定向:tests/test_task_memory.py                                            → 31 passed
定向:tests/test_resume.py + test_resume_crash.py + test_loop_state.py      → 57 passed (0:00:32)
定向:tool_capabilities + docs_anchors + repo_map + plan_invariants +
      single_shot + provenance_anchor + driver + experiment_* + plan_stage +
      plain_loop + context_wiring + loop_state + evidence_store           → 151 passed (0:02:43)
全量第一轮:.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b2-g06full
      → **2 failed, 1058 passed, 4 skipped, 4 warnings in 1488.59s (0:24:48)**,EXIT=1
      (`test_graph_resume_finishes_and_reruns_every_gate`、
      `test_resume_survives_an_unclean_process_death` 都收敛成 NEEDS_REVIEW)
全量第二轮(修完幂等):.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b2-g06full2
      → **1 failed, 1060 passed, 4 skipped, 4 warnings in 1562.66s (0:26:02)**,EXIT=1
      (唯一一条红是 `test_docs_anchors` —— 我新增的幂等用例让 ADR-0013 的三条锚点行号漂移;
      重钉后 `test_docs_anchors` 6 passed,代码一字未动)
全量第三轮(锚点重钉后):.venv/Scripts/pytest.exe -q --basetemp=D:/tmp/pt-upgrade-b2-g06full3
      → **1061 passed, 4 skipped, 4 warnings in 1403.81s (0:23:23)**,EXIT=0
```

**第一轮那两条红是我自己造的缺陷,不是既有回归。** 根因:`TaskMemory.set_context()` 无条件
`version += 1`,而恢复路径(`restore_runtime → _open_agent_state`)会拿同一份冻结输入重放它 ——
版本一升,记忆 hash 就和崩溃前那一份不同,于是我新加的绑定校验**把自己的恢复判成 mismatch**,
两条 resume 用例被错误收敛成待审。修法:内容真的变了才升版本(幂等重放不升),
并由 `test_set_context_is_idempotent_for_the_same_frozen_input` 单独钉住,
docstring 里写明"这就是本轮真实红过的两条用例的根因"。
教训与 G03 那条同源:**新增一条拒绝路径,必须先用既有的通过用例跑一遍**——
拒绝面最容易在"本该放行"的路径上误伤,而误伤表现为待审(保守)而不是崩溃,不容易一眼看出。

数对账:G05 收口的全量是 `1030 passed / 4 skipped`;G06 新增 31 条(`test_task_memory.py`)
⇒ 期望 `1061 passed`。第二轮已经数到 `1060 passed + 1 failed`(那一条是锚点行号),
重钉后第三轮实测 `1061 passed`,逐条闭合。skip 仍是同一组 4 条(符号链接权限 ×2、Redis 不可用 ×2),
新增 skip 为 0;warnings 同为 4 类,与 G05 一致。日志
`D:/tmp/pt-upgrade-b2-g06full.log`、`-g06full2.log`、`-g06full3.log`
(本机 GBK 码页,读数按 gbk+replace 解码取)。耗时 24:48 / 26:02 按本机负载记账,不作回归结论。

旧断言处理:一条没删、一条没放宽。`test_repo_map.py` 的三条形状守卫
(`extra_system=self._persistent_context(self.workspace),` 逐字保留、关闭骨架时仍为 `["", ""]`、
候选点仍只带变体提示)全部原样通过——为此把装配收进 `_begin_assembly`,
`_persistent_context` 只做"装配过就用装配、没用过就用骨架"。
`test_tool_capabilities.py` 18 条、`test_plan_invariants.py` 的 PLAN 可见面断言、
`test_single_shot.py` 的两臂名单(比的是导入常量)原样通过。
唯一被我的实现改动过的既有断言是 `test_docs_anchors.py` 的 nodes.py **行索引** 700→849
(文档正文与被钉句子一字未动,这是该机制规定的维护动作)。
`test_graph_resume_finishes_and_reruns_every_gate` 与 `test_resume_survives_an_unclean_process_death`
的**断言一字未动**——第一轮红的原因在我这边(见上面「检查读数」里那条根因),修的是实现不是用例。

### 需要点名的范围决定与判断

1. **`state.py` 与 `prompts.py` 未动**:会话与记忆是**闭包对象**,不进 LangGraph state
   (state 必须可 SqliteSaver 序列化,`SessionState` 里有锁与可变列表);模型可见文本
   全部经 `_begin_assembly` 的装配面进来,没有新提示词。**这是范围决定,不是遗漏。**
2. **`update_memory` 不进 PLAN 阶段**:PLAN 的可见面仍然只有 `finish`。给它一个可调用手段
   等于重开 G01 刚堵掉的洞(合规纯文本必须一次请求就结束)。计划文本由平台代登记(`record_plan`)。
3. **两臂对称**:`update_memory` 同时进 `READ_TOOLS`/`WRITE_TOOLS`/`ONE_SHOT_TOOLS`,
   消融变量仍只有"有无执行反馈 / 有无重试"。
4. **未绑定快照不判 mismatch**:否则升级前留下的每一份快照一律待审,那是伪造迁移成功。
5. **装配幂等 ⇒ 同一阶段内新增观测不会立刻进 system**(跨阶段/跨轮次才重装配)。
   选择幂等是为了保住 stable prefix;这条写进 ADR-0013 的反方节,不伪装成"证据自动同步"。
6. **候选 ctx 不共享记忆与证据**:候选用 `report_dir/cand{i}`,索引各自独立。
   G13 要传证据包时先定"合法共享域"。

### 剩余限制

1. 记忆块与骨架(、G08 的项目说明)抢同一份 system 预算;`task_memory_max_chars=3000` 是拍的起点,
   G07 抬窗口时三者必须一起标定;
2. 观测是**索引不是正文**:`[E1] … 行 1-400` 不带内容。模型可能因为"只看到索引"再读一遍,
   省不省请求离线不知道(G17);
3. `MEMORY_SCHEMA_VERSION=1` 无迁移路径:版本不符一律当没有记忆(保守冷启),
   不做"猜着升版";
4. `loop_state.json` 未升 `SNAPSHOT_VERSION`,依赖"新增字段全可空"这一前提;
   真要改语义必须同时升这个数;
5. `SessionState.calls` 落盘时截尾 200 条(快照体积保护),`orphan_tool_calls` 读的是
   **即将发出的消息**而不是这本账,所以裁剪不影响判定;
6. 离线全绿只证明接线与形状:**不证明修复率、不证明重复调查变少、不证明省 token**。

### 费用与外部动作

零模型调用、零付费、零网络、零 push、零删除历史结果。

### 下一卡

G07(模型能力 profile 与 repair_v2 上下文策略):`app/llm/profiles.py`、`app/agent/runtime.py`(新增)、
`app/context/token_window.py`、`app/config.py`、`task_spec.py`、`resources.py`、`runner.py`、`nodes.py`、
`plain_loop.py`、`driver.py`、`run_single.py`、`provenance.py`、`app/api/service.py` + `test_model_profiles.py`。
G07 必须一并处理的登记项:`refine_head_lines/tail_lines` 与 `task_memory_max_chars`、
`repo_map_max_chars` 的联动标定;`TransportRetryPolicy`/`RequestEstimate` 的生产装配点;
`served_model` 采集;`repetition` 落进运行产物。

## G07 / P1:模型能力 profile 与 repair_v2 上下文策略

### 范围补充(先声明再动手)

SPEC §7 G07 的文件清单里没有 `app/llm/openai_client.py` 与 `app/llm/usage.py`,但这一卡的两件事
只能落在这两个文件里,按"越界前先更新当前卡的范围说明"的规矩在此登记:

- `app/llm/usage.py`:`ModelResponseEnvelope` 是"一次响应的全部事实"的唯一载体,`served_model`
  是响应字段,只能挂在这里(挂到 `ModelProfile` 上就等于把观测写成了声明)。
- `app/llm/openai_client.py`:`served_model` 的采集点(响应到手、业务解释之前)与
  `TransportRetryPolicy` 的生产装配点(G02 留下的"无调用方"开关)都在这个文件。
  两处都按"默认值 = 与今日逐字相同"实现:新键 `llm_platform_retries=0`,未声明能力走保守档。
