# 付费批预登记(2026-10-08):题单、判据、停手条件先于任何一次付费运行入库

**本文档的作用**:把"要花钱的那一批"设计死在纸面上。任何一次真实模型运行**之前**,本文档必须已经进
git;跑完之后只允许在下面追加"实测结果"一节,不允许改判据、题单或分母。
上一档的教训写在 `docs/swe-ablation-evidence-2026-10-07.md`:我给消融臂私加了一个没登记的轮次上限,
结果 777k tokens 买回一个 0/7 的空结果 —— **实验设计缺陷会把结论污染成设计的函数**。

## 一、这批要回答什么(以及不回答什么)

| 编号 | 问题 | 唯一登记的变量 | 主指标 |
| --- | --- | --- | --- |
| Q0 | 同参重复的运行之间结果差多大(噪声地板) | 无(两臂都不做,只做重复) | 每题 3 次里 resolved 次数的分布 |
| Q1 | agent 循环相对单发补丁有没有净增益 | `--arm agent` vs `--arm one_shot` | 多数票口径的 resolved 题数差 |
| Q2 | 工作记忆压缩值不值 | `context_window_tokens` 16000 vs 0(0 = 关闭) | tokens p50;resolved 只作副作用监控 |
| Q3 | 混单位门禁要不要统一到 provider 真值 | `token_estimate_factor` 1.0 vs 1.47 | 判死率与 tokens p50(预期是"更早死",不是"修得更多") |

**不回答**:与 gold 补丁的结构相似度(`app/evals/metrics.py` 的判定语义不动,这是禁区);
换模型后的能力主张(没授权);修复率的小数点排名。

## 二、为什么必须先跑 Q0:上一批的失败不是运气

已入库的实测事实,是这条设计的直接依据:

- **同题两次独立运行会死在不同阶段**。`sphinx-7590`(107 行 / 3 文件)在 `runs/swe-hard-graph2` 与
  `runs/swe-hard-graph3` 两次同参运行:一次定位段触份额顶后降级、补丁阶段 13 次 search / 10 次 read
  仍未提交补丁;另一次进了 PROPOSE 并跑到任务级总额。两次都 failed,但**失败形状不同**。
- **要检测的效应比这个抖动小一个数量级**。M15 的静态反事实量出:生产默认 16000/keep=6 的累计额度
  节省中位只有 **6%**(触发实例),而补丁段那 2 次判死在压缩下是"一次能活、一次仍死"(M16,n=2)。
  对照同一题两臂/两次的真实用量:`astropy-8707` 250,175 vs 405,427 tokens(1.6 倍),
  `xarray-3095` 226,868 vs 320,386(1.4 倍)—— **6% 落在这种摆幅里根本分辨不出来**。

⇒ 结论:**不知道 σ,任何两臂差值都不能解读。** Q0 花掉的钱买的不是分数,是后面三个问题的可解释性。
如果 Q0 显示这道题在 3 次里从不翻转,那它就是"不可判别题",不进 Q1-Q3 —— 省下的是对照臂的整份预算。

## 三、题单(7 道,全部已导入镜像且金补丁冒烟可修)

选题规则沿用已入库的那五条(非最易档 / 补丁 >50 行或多文件 / 文件数 ≤5 / P2P 与 F2P 规模上限 /
测试 id 先过平台白名单),`docs/swe-ablation-evidence-2026-10-07.md` 是规则的原始出处。

| 题 | 改动行(add+del) | 文件 | 已跑过的历史 |
| --- | --- | --- | --- |
| `SWE-sphinx-doc__sphinx-7590` | 107 | 3 | 真实臂 2 次 failed(死因见上),消融臂未跑(按剔除规则不补) |
| `SWE-sphinx-doc__sphinx-7748` | 90 | 1 | 真实臂 failed,定位段爆预算(修复前) |
| `SWE-astropy__astropy-8707` | 75 | 2 | 真实臂 **resolved** 250,175 tok;消融臂 failed 405,427 tok |
| `SWE-sphinx-doc__sphinx-8593` | 60 | 2 | 未跑 |
| `SWE-sphinx-doc__sphinx-9461` | 52 | 3 | 未跑 |
| `SWE-sphinx-doc__sphinx-8548` | 50 | 2 | 未跑 |
| `SWE-pydata__xarray-3095` | 49 | 2 | 两臂 **都 resolved**(226,868 vs 320,386 tok) |

"改动行"的口径与 `docs/swe-ablation-evidence-2026-10-07.md` 一致(金补丁的 `+` 与 `-` 行数相加,
不含 `+++`/`---` 头),数字由 `bugs/<题>/expected/reference.diff` 现算,不是抄表。

**明确排除,不算进分母**(排除理由与出处照写,不许事后拿它们凑数):
`scikit-learn-12682`(金补丁在本协议下无法表示,已隔离到 quarantine,见 `ref-blockpatch-anchor-limit`
那条边界)、`astropy-13398`(容器基线里失败测试自身报错,题目形态不符)、`matplotlib-24870`(未导入)。
`SWE-astropy__astropy-8707` 与 `SWE-pydata__xarray-3095` 保留在题单里,但它们**历史上有 resolved 记录**,
所以 Q1 的判据里必须区分"新证据"与"重复旧证据"。

## 四、统一口径(所有臂、所有重复完全同参,只许改登记表里那一个变量)

```bash
# 逐题串行执行(付费任务禁止同题并发,这条已付过学费)
PYTHONPATH=. .venv/Scripts/python.exe app/evals/run_single.py \
  --bug SWE-sphinx-doc__sphinx-9461 --model openai --engine graph --arm agent \
  --max-turns 24 --out runs/q0-noise-2026-10-08
```

环境变量(两臂同值,逐题落进 `provenance.config_snapshot`):
`PATCHPILOT_LLM_ENABLED=true`、`PATCHPILOT_TOKEN_BUDGET=400000`、
`PATCHPILOT_TASK_TIMEOUT_SECONDS=1800`、`PATCHPILOT_EXECUTION_BACKEND=docker`、
`PATCHPILOT_LOCALIZE_BUDGET_SHARE=0.6`、`PATCHPILOT_PLAN_BUDGET_SHARE=0.15`、
`PATCHPILOT_CONTEXT_WINDOW_TOKENS=16000`、`PATCHPILOT_CONTEXT_KEEP_RECENT_TURNS=6`、
`PATCHPILOT_TOKEN_ESTIMATE_FACTOR=1.0`。

- **零成本冒烟先行**:每个臂先用 `--model fake` 同一套参跑一遍,确认参数接线与产物目录形状正确
  (`scripts/compare_batches.py` 可比判定字段),再开真实模型。上一批就是靠这一步省下了无效对照。
- 判定口径不动:平台 verdict(resolved / failed / needs_review),`app/evals/metrics.py` 一字不改。
- 消融变量全集(有开关才许当变量):`--arm`、`context_window_tokens`、`context_keep_recent_turns`、
  `plan_stage_enabled`、`repo_map_enabled`、`adaptive_branching_enabled`、`localize_budget_share`、
  `plan_budget_share`、`token_estimate_factor`、`loop_snapshot_enabled`。
  **M4 的失败反思没有开关**(`app/graph/reflection.py::with_discarded_patch` 无条件生效),
  所以它**不做对照** —— 要测它得先加配置,那是另一张卡,不在本批里顺手改。

## 五、判据(现在写死,跑完之后不许动)

**Q0 噪声地板**(7 题 × 3 次同参 = 21 次真实运行):
- 记 `res_i` = 第 i 题 3 次里 resolved 的次数(0..3)。
- **可判别题** = `res_i ∈ {1, 2}` 的题(结果会翻转,才可能被机制改变)。
- 判据:可判别题数 `k ≥ 2` ⇒ 才允许启动 Q1;`k ≤ 1` ⇒ **停止付费**,结论写成
  "deepseek-flash 在这档难度上的结果方差淹没有望检测的机制差异,本证据集不支持任何净增益主张"。
- 顺带产出 tokens 的逐题极差与 p50,作为后面三个实验的分辨率参照。

**Q1 循环净增益**(只在可判别题上,两臂 × k 题 × 3 次 = 6k 次):
- 每题每臂取**多数票**(3 次里 ≥2 次 resolved ⇒ 该臂该题记 1)。
- 令 `Δ = |真实臂多数票| − |消融臂多数票|`(同题配对,只在两臂都跑过的题上算)。
- `Δ ≥ 2` ⇒ 可写"在本证据集内循环有净增益";`Δ ≤ 1` ⇒ 写"未证明";
  `Δ ≤ −2` ⇒ 写"循环在本证据集内是净负担",这条同样是合格结论,照发。
- **剔除规则不变**:两臂共用同一段代码、同一份提示、同一工具集的公共阶段(LOCALIZE)里耗尽额度的题,
  不可能产生差值 ⇒ 记为"同段同因失败",不补跑对照臂。
- 每臂每题为 n=3 而非 n=1:上一批"每臂 n=1 分辨不了机制与运气"是本判据要修的正是这一点。

**Q2 压缩值不值** / **Q3 门禁换算**(各 2 臂 × k 题 × 3 次,一次批只做一个):
- 主指标是 **tokens p50 与判死率**,不是 resolved。理由:M15 的静态上界只 6%,
  把它当 resolved 的主张去检验就是拿一个测不出来的效应去花钱。
- Q2 的合格结论有三种,都算买到东西:①真实批的节省与静态反事实同量级 ⇒ 反事实机器有预测力;
  ②节省远小于 6% ⇒ 静态反事实**低估**了模型行为的自适应(看到更短上下文会多查),
  这条要写进"已知边界";③无差异 ⇒ 16000 是装饰,抬到额度优先才有意义。
- Q3 的预期方向是"更早死、更省钱",**不是**修得更多;若 resolved 反而下降,那是门禁提前判死的代价,
  照实写。

**通用**:分母 = 设计里的运行次数,任何运行失败(容器、网络、400)都**计入分母并按 failed 处理**,
不重试、不摘除;唯一例外是"从未发起模型调用就崩"的运行时,那种情况不计入分母但要单独列出来。

## 六、停手条件(触发即终止整批,不解释、不抢救)

1. 任何一次运行返回 `400`(思考模式 `reasoning_content` 类的协议错误)或 `report.turns == 0`
   而轨迹显示已发出请求(即"花了钱不入账",出处 `4a4093e`)⇒ 立刻停批。
2. 同一题并发出现两个在跑的 run_dir ⇒ 立刻停(这条我在 2026-10-07 违反过一次,多烧了一轮调用)。
3. 累计 tokens 触到本批预算上限 ⇒ 停(见第七节的数)。
4. Q0 判据不满足 ⇒ 不启动 Q1(这是最省钱的停手条件,不是失败)。

## 七、成本与耗时(用已回填的价目表口径,不是猜的)

- 单价基准:25 份历史付费 run 合计 **4,336,298 tokens ≈ $1.46** ⇒ 约 **$0.34 / 百万 tokens**
  (`deepseek-flash` peak 档 $0.30/$1.20 的约值,off-peak 对半;限定条件见
  `docs/interview-evidence-2026-10-07.md` 的"不能写"一节)。
- 单题均价按第二档真实臂实测 **≈344k tokens**(1,033k / 3 题),上限受 `token_budget=400k` 约束。
- **Q0**:21 次 × 344k ≈ **7.2M tokens ≈ $2.4**;墙钟按容器内单题 4–6 分钟串行估 **1.5–2.5 小时**。
- **Q1**(k=2 时):12 次 ≈ **4.1M tokens ≈ $1.4**;k=3 时 18 次 ≈ **6.2M ≈ $2.1**。
- 整批上限(含 Q2 或 Q3 之一)取 **20M tokens ≈ $6.8**;触顶即停,不追加。
- 钱不是这里的约束,**每次付费批只能验证一个变量**才是:所以本批把 Q0 排在最前面,
  把 Q1 限定在可判别题上,把 Q2/Q3 各只留一个槽位。

## 八、跑完之后必须落的东西

1. 在本文档末尾追加"实测结果"一节(逐题逐次一行:resolved / tokens / turns / 判死类型),
   原始产物留 `runs/` 并在 `docs/evidence/` 放脚本导出的汇总。
2. 判死类型沿用 M15 的分类法(额度闸 / 回合耗尽),从 `report.error` 原文取,不按 status 名猜。
3. 负面结果照发。上一批买到的最有价值的东西就是三条缺陷和一个负面结论,不是一个好看的分数。

---

## 实测结果(补记 A,2026-10-09:gen1 中止与 thinking 钉死——先于 gen2 任何付费运行入库)

**gen1(2026-10-09 12:29–12:58,server-default 思考模式)中止,Q0 作废重跑为 gen2。**
判据、题单、分母(7 题 × 3 次)一字未改;改动只有环境变量一枚:PATCHPILOT_LLM_THINKING=disabled
(预登记第四节原口径是"不设该变量 = 跟随服务端缺省",该缺省已被端点侧单方面改掉,见下)。

### 时间线与花费

- fake 冒烟(同参,零成本):7/7 resolved,登记参数逐字入 provenance,compare_batches 可解析。
- gen1 真实尝试 8 次(台账 runs/paid-batch-ledger-2026-10-09-gen1-aborted.jsonl,原始产物
  runs/q0-noise-2026-10-08-gen1-aborted/):7590×3 全 BUDGET_EXCEEDED(383k/386k/400k)、
  7748×3 全 BUDGET_EXCEEDED(405k/375k/390k)、8707×1 NEEDS_REVIEW(109k);
  另有 1 次驱动修复期间被人工停止(7748,8 turns,轨迹重建 ≈59k tokens,单独列示)。
  **gen1 合计 ≈2.56M tokens ≈ $0.87,全部计入整批 20M 上限的已花费。**
- 驱动器两处执行 bug 同批修掉(commit be6ad0b/c3cde25):400 判据误匹配预算数字"400000";
  台账续跑未按 model 过滤(fake 冒烟条目占走真实槽位)。台账含事故条目,报告期单独列示。

### 停手原因(预登记停手条件 1 的真实触发,与处置)

8707 rep1 于 PLAN 段第 7 次请求收到 `Error code: 400 - The reasoning_content in the
thinking mode must be passed back to the API`,驱动器按停手条件 1 立即停批(行为正确)。

**诊断(探针 ≈80k tokens,脚本 runs/probe_*.py):**
1. 客户端**没有**丢字段:失败请求的 loop_state 快照逐条含 reasoning_content(带工具轮与纯文本轮
   都有),零次压缩(无 context_compact 事件),桩路径未参与;
2. 逐字重放该请求 → 400 稳定复现;二分到 `[system, user, 单条 assistant]` 仍 400;
3. **同一请求在 40 分钟后重发 → 200**(probe_mutate.py T0):端点对该校验是**间歇性**的
   (灰度/负载型),不存在可确定性规避的客户端请求形状——plain_loop 的催促循环(连续 assistant
   消息)只是让触发概率显形,不是缺陷;
4. `thinking=disabled` 下端点每轮 `reasoning_content=None`(probe_disabled_effect.py 两连轮实证),
   思维链从不进入客户端历史,间歇校验无从触发;同一失败请求加 disabled 即 200(P3)。

**结论:2026-10-07 批次运行时的"服务端缺省=非思考"前提已被端点单方面改掉。** 把缺省显式钉回
非思考(=disabled)是对登记条件最忠实的复原,且不触碰循环语义、不引入未登记的消息形状变化。
gen1 的 7 次完成尝试跑在"思考常开"条件下,与 gen2 不同条件,**不并入判据分母**,只作废为学费
(其本身复读出一个事实:两道难题 6/6 全额度判死,与 10-07 的历史死法一致)。
