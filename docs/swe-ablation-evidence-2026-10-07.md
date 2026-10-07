# SWE 真实题两臂消融对照(2026-10-07)

## 要回答的问题

`runs/swe-real` 里真实模型(deepseek-flash)在 5 道 SWE-bench Verified 题上 5/5 resolved。
这到底证明的是"平台的 agent 循环能修 bug",还是只证明"链路完整、模型本来就会"?
唯一能区分二者的办法是消融:同模型、同题、同判定,把循环里的机制拿掉再量一次。

## 两臂定义

| | 真实臂 | 消融臂 |
| --- | --- | --- |
| 入口 | `run_single --engine graph` | `run_single --engine plain --arm one_shot` |
| 批次目录 | `runs/swe-real` | `runs/swe-oneshot-1` |
| 定位阶段 | `LOCALIZE_PROMPT` + `READ_TOOLS`,max_turns 12 | **完全相同**(同一份提示与工具集) |
| 补丁阶段 | `run_tests` 可用,VERIFY 失败可回滚重试(最多 5 轮) | **无 `run_tests`**,VERIFY 失败即终局 |
| 基线 / 验证 / 门禁 / 判定 | `run_task` 与 `run_task_graph` 的既有路径 | **同一条代码路径**(靠 `run_task` 的 `agent` 执行体插槽复用) |

刻意只消融两处,且判定不分叉:两臂之差才可以说成机制贡献,而不是口径差异。
`report.json` 与 `provenance` 记录 `arm` 字段,批次报告的复现命令自动带 `--arm`。

## 结果:消融臂同样 5/5,代价只要一半

指标口径为 `load_run → annotate → compute_metrics` 三段齐全(派生字段只在 annotate 后存在)。

| 指标 | 真实臂 | 消融臂 |
| --- | --- | --- |
| final_resolution_rate | 1.0(5/5) | **1.0(5/5)** |
| localization_rate | 1.0 | 1.0 |
| patch_application_rate | 1.0 | 1.0 |
| regression_introduction_rate | 0.0 | 0.0 |
| security_blocked_count | 0 | 0 |
| avg_tokens | 100,738 | **54,098** |
| avg_duration_ms | 141,850 | **67,255** |
| turns 区间 | 8–21 | 8–11 |

消融臂全程没有执行过任何一次 `run_tests`(逐题轨迹:0 次执行;其中 pytest-7205 有 1 次尝试被阶段规则挡下)。
也就是说模型是在**完全不知道自己补丁对不对**的情况下提交,而平台独立验证给出 5/5。
`apply_patch` 尝试次数:消融臂 5 题共 6 次(flask 有 1 次因锚定上下文不唯一被拒后自纠,其余各 1 次),
真实臂 5 题各 1 次——两臂都拿到协议/门禁层的形式反馈,被拿掉的只有测试执行结果这一层。

补丁形状逐题对照(`+/-` 为增删行数,gold 取题目 `expected/reference.diff`):

| 题 | 两臂补丁是否逐字节一致 | 真实臂 | 消融臂 | gold |
| --- | --- | --- | --- | --- |
| flask-5014 | 一致 | 3/0 | 3/0 | 3/0 |
| requests-1142 | 不一致 | 9/7 | 0/1 | 2/1 |
| pylint-6903 | 不一致 | 6/1 | 2/2 | 7/0 |
| pytest-5809 | 一致 | 1/1 | 1/1 | 1/5 |
| pytest-7205 | 一致 | 3/1 | 3/1 | 2/1 |

## 读数:这次实验证明了什么、没证明什么

**证明了**:5/5 的证据强度不来自 agent 循环。这批题对 deepseek-flash 属于"一次成型即可解"的一档,
平台的贡献在这批数据上体现为**可核验性**——门禁一次都没被绕过、测试在题目自带的时代正确环境里真跑、
判定不采信模型自述(`finish(success=true)` 只是声明,resolved 由平台 VERIFY 给出)。

**没证明**的有四条,必须一起读:

1. **样本是我挑的最易档**。这 5 题 100% 属于官方"<15 分钟"难度档、全部单文件、补丁 14–20 行;
   而数据集里 54% 的题补丁比我最难的那道更大、14% 是多文件(实测口径见
   `runs/swe-evidence-2026-10-06/selection_bias.txt`)。因此"单发打平循环"**不可外推到一般难度**。
2. **跨轮重试从未被检验**。真实臂 5 题的 `rounds` 全是 1,回滚重跑这条路一次都没走过;
   本实验消融掉的是"本来就没用上的机制"。自适应分支在 35 题回放里同样 0 次触发。
3. **两臂的判定强度不完全对称**。真实臂走 graph,含 verify 双跑一致性检查(防测试本身不稳定);
   消融臂走 plain 驱动器,没有这一道。消融臂的 resolved 证据略弱,要靠重复跑补。
4. **"测试全绿"不等于"修对了"**——pylint-6903 是活样本:gold 把"可用 CPU 数为 0"兜到 1,
   消融臂改的是把 0 变成 `None`(交给另一条分支),F2P 与 P2P 照样全绿,判 resolved。
   这是平台判定规则的**能力边界**:它能保证"补丁在声明的测试集上成立且范围合法",
   不能保证"补丁与上游修法等价"。记录在此,不做美化。

## 下一步(要让"循环有净贡献"成为可写的结论)

按性价比排:

1. **换样本而不是换机制**:按难度预选 5 题(补丁 >50 行或多文件,官方估时 ≥15 分钟),两臂同跑。
   只有当消融臂掉下来、真实臂还站着,差值才是 agent 循环的证据。
2. **每题 3 次重复**取方差(消融臂便宜,5 题 ×3 ≈ 单个真实批的 token 量),顺带补上第 3 条局限。
3. 判定规则的边界(第 4 条)若要强化,方向是"与 gold 补丁做结构对比并报告偏差",
   但这属于新的判定语义,须先讨论再动 `app/evals/metrics.py`(禁区)。

## 下一步的预登记(在导入与跑批之前写死,git 时间戳即证据)

本节的题单与判据先于任何运行结果提交。跑批后不得增删题目、不得改判据。

**题源**:SWE-bench Verified 全 500 题元数据(HF datasets-server,`princeton-nlp/SWE-bench_Verified`,
本地缓存 `runs/swe-hard-2026-10-07/verified_rows.json`)。

**入选规则(静态、与模型无关)**:

- R1 `difficulty` ≠ `<15 min fix`;
- R2 补丁增删行数 > 50 **或** 触碰文件数 ≥ 2;
- R3 触碰文件数 ≤ 5(超出 `max_patch_files` 属门禁按设计拦截,不是能力差异);
- R4 `1 ≤ |PASS_TO_PASS| ≤ 300` 且 `1 ≤ |FAIL_TO_PASS| ≤ 30`(必须有绿灯探针;单次 pytest 预算跑得完。
  导入器仍按 `--max-p2p 50` 保留上限,与第一批 5 题同口径);
- R5 F2P ∪ P2P 每一条都过平台测试 id 白名单且路径锚定(含 `::`)——即复用 `app/evals/bugset.py`
  的那一套护栏作为**选择**条件,而不是事后发现被拒。

排序键 `(-补丁行数, instance_id)`,取前 10:

```text
astropy__astropy-13398              astropy/astropy           1-4 hours        215 行 / 4 文件
sphinx-doc__sphinx-7590             sphinx-doc/sphinx         >4 hours         107 行 / 3 文件
scikit-learn__scikit-learn-12682    scikit-learn/scikit-learn 15 min - 1 hour   90 行 / 2 文件
sphinx-doc__sphinx-7748             sphinx-doc/sphinx         15 min - 1 hour   90 行 / 1 文件
astropy__astropy-8707               astropy/astropy           15 min - 1 hour   75 行 / 2 文件
sphinx-doc__sphinx-8593             sphinx-doc/sphinx         15 min - 1 hour   60 行 / 2 文件
sphinx-doc__sphinx-9461             sphinx-doc/sphinx         1-4 hours         52 行 / 3 文件
sphinx-doc__sphinx-8548             sphinx-doc/sphinx         1-4 hours         50 行 / 2 文件
pydata__xarray-3095                 pydata/xarray             15 min - 1 hour   49 行 / 2 文件
matplotlib__matplotlib-24870        matplotlib/matplotlib     15 min - 1 hour   33 行 / 2 文件
```

500 题的静态账目:R1 排除 194、R2 排除 234、R3 排除 2、R4 排除 13、R5 排除 40,合格 17(取前 10)。
对照组形状:第一批 5 题补丁 14–20 行、全部单文件、100% `<15 min fix`;本批 33–215 行、9/10 多文件、0 题属最易档。

**协议**:两臂(`graph` 真实臂 / `plain + --arm one_shot` 消融臂)在同一批题上各跑;
环境不可证的题**留在分母里并报拒绝原因**,不许悄悄剔除。

**预先写死的判据**:

- 若消融臂与真实臂的 resolved 数相差 ≤ 1 → 结论仍是"循环在本项目证据集内无净增贡献",
  下一步该改的是机制或样本定义,而不是再多跑几轮;
- 若真实臂比消融臂多 resolved ≥ 2 题 → 才算拿到"循环有净贡献"的证据,并同时报告 token 代价;
- 任一臂出现"补丁触碰期望文件却没修好"或"修好但引入回归"都要单列,不许合并成一个百分比。

## 难样本第一轮结果:消融臂 0/7,但这是臂的缺陷,不是能力结论

第一档付费跑(`runs/swe-hard-oneshot`,7 题,约 777k tokens)全部未 resolved,`changed_files` 为空。
逐题轨迹给出的原因是三类,没有一类说明"模型修不出":

| 失败签名 | 题数 | 事实依据(`trajectory.jsonl` 按 state 数 `tool=="llm"` 事件) |
| --- | --- | --- |
| LOCALIZE 恰好打满 12 轮、从未调用 `finish` | 5 | 模型一直在 `search_code`/`read_file`,被轮次上界切断,根本没进补丁阶段 |
| 过了定位,补丁段打满 6 轮 | 2 | 6 轮上界是**本臂私加的 handicap**(真实臂同阶段是 12 轮);astropy 一题试了 2 次 `apply_patch` 未落地 |
| 端点 400 崩溃 | 1 | `reasoning_content ... must be passed back to the API`,见下 |

两条由此暴露的缺陷,均已修(带零网络用例):

1. **臂的形状不公平**:`_PROPOSE_TURNS = 6` 不在预登记的两个消融变量里。消融实验里任何未登记的
   额外限制都会把结论变成实验参数的函数。已改为两阶段与真实臂同轮次上界
   (`app/evals/single_shot.py`,不变量钉在 `tests/test_single_shot.py`)。
2. **思考模式的思维链没有回传**:`app/llm/openai_client.py` 丢弃端点返回的 `reasoning_content`,
   而 DeepSeek 类端点要求后续请求原样带回,缺了就 400。更糟的是 400 走崩溃分支
   (`NEEDS_REVIEW`),该任务的 `turns/tokens` 记 0——**花了钱不入账**。
   修复:`AssistantTurn.reasoning_content` 字段 + `run_plain_loop` 组装 assistant 消息时带上
   (`tests/test_llm.py`、`tests/test_plain_loop.py` 各钉一例,含"非思考端点消息形态不变")。
3. **`--max-turns 12` 在真实多文件题上是硬约束**:第一批 5 题定位只用 3–9 轮,这批 7 题里 5 题
   直接打满 12 轮。也就是说这个参数本身在限制模型表现,不放宽它就无法测出能力上限。
   第二档两臂统一放宽到 **24 轮/段、`token_budget` 400k、`task_timeout` 1800s**(三个值两臂完全
   同口径,并由 `provenance.config_snapshot` 逐题记录);先跑 3 题真实臂试水,确认平台+模型在这档
   难度上至少能修出一题,再补齐两臂。

金补丁冒烟(零成本)另给出可用题集:预登记 10 → 可证 8 → **金补丁在平台里可修 7**
(`scikit-learn-12682` 回放后 `diff is empty`,属夹具问题,排除;`astropy-13398` 容器基线不符;
`matplotlib-24870` 未导入)。分母仍按 10 报告。

## 顺带查出的块协议真实边界:有些上游金补丁在本协议下无法表示

`scikit-learn-12682` 金补丁冒烟失败(`PATCH_REJECTED / [format] diff is empty`)追到的根因不是夹具写坏,
而是块协议的锚定规则撞上了真实补丁:

```text
BlockPatchError: [context] ambiguous_anchor:
  sklearn/decomposition/dict_learning.py 的上下文行在文件中出现 2 次
```

- 协议要求上下文行**全文件唯一**(卡 1 的设计:靠内容定位,不靠行号)。而这段上游补丁只带 3 行上下文,
  在那个 1600 行文件里正好重复出现 → `git apply` 能靠行号应用,块协议不能。
- **对模型不成问题**:拒因里带了"出现 2 次"和补救指令(补充更多上下文行),模型下一轮可以自己加宽上下文。
- **对机械转换是硬限制**:把固定金补丁逐字转成块文本时,转换器无权编造额外上下文行。
  所以这类题**不能进题目集**——`expected/reference.diff` 与回放语料都无法过 round-trip 不变量。

处置:该题目录隔离到 `runs/swe-hard-2026-10-07/quarantine/SWE-scikit-learn__scikit-learn-12682/`
(证据保留,不进 `bugs/`,`tests/test_blockpatch.py` 的语料口径一行未改,复跑 90 通过)。
如果以后要接这类题,方向是给转换器加"自动向上下文两侧扩行直到唯一"的能力——
那是 `app/gitops/blockpatch.py` 的语义变更,属禁区,需要先讨论。

## 难样本第二档:两臂可比对只有 2 对,判据未被满足

口径(两臂逐字同参):`--max-turns 24`(每段)、`PATCHPILOT_TOKEN_BUDGET=400000`、
`PATCHPILOT_TASK_TIMEOUT_SECONDS=1800`、`execution_backend=docker`、`--model openai`(= deepseek-flash)。
三值都由 `provenance.config_snapshot` 逐题记录。产物目录:真实臂 `runs/swe-hard-graph`,
消融臂 `runs/swe-hard-oneshot2`(修复 handicap 后独立成批,不与第一档混目录)。

| 题(补丁规模) | 真实臂 | 消融臂 | 说明 |
| --- | --- | --- | --- |
| `astropy-8707` 75 行 / 2 文件 | **resolved** 250,175 tok / 21 轮(定位 11 + 补丁 10) | **failed** 405,427 tok / 定位 19 轮耗尽预算,`apply_patch` 0 次 | 两臂唯一一处结果差异 |
| `xarray-3095` 49 行 / 2 文件 | **resolved** 226,868 tok | **resolved** 320,386 tok(定位 18 + 补丁 5,全程未跑测试) | 消融臂反而更贵 |
| `sphinx-7590` 107 行 / 3 文件 | failed,定位段 417,894 tok 爆预算 | 未跑 | 见下方剔除规则 |
| `sphinx-7748` 90 行 / 1 文件 | failed,定位段 418,802 tok 爆预算 | 未跑 | 同上 |

**按预先登记的判据读**:可比对 n=2,真实臂 2/2、消融臂 1/2,差值 = 1 → 落在"差 ≤1 ⇒ 循环在本证据集内
无净增贡献"这一侧,**没有拿到净贡献证据**。

**而且那唯一一处差异不足以算作机制证据**:`astropy-8707` 消融臂是死在定位段耗尽 400k,而定位段两臂共用
同一段代码、同一份提示、同一个工具集——真实臂同题只用 11 轮就收束,消融臂用了 19 轮。差别来自模型采样的
随机性,不是被消融的机制。单次运行(每臂 n=1)分辨不了这一点,这是本节结论的硬上限。

**剔除规则(为省钱也为口径)**:真实臂死在 LOCALIZE 的题不再跑消融臂——同段同因失败不可能产生差值。
`sphinx-7590`、`sphinx-7748` 据此排除,记为"两臂同段同因失败",不算作消融臂的失败样本。
另有 `sphinx-8593/9461/8548` 三道同仓未跑(同一失败模式的先验概率高)。

**本轮最硬的产出不是分数,是瓶颈定位**:这一档难度的实际约束是**定位阶段的 token 消耗**。
4 次卡在定位段的运行全部是"只读调查吃满 400k、从未进入补丁阶段"。代码层面能对上:
`app/graph/nodes.py` 的 `_token_budget_for` 把 `token_budget - 已用` **整份**交给定位段,
没有任何"给补丁阶段留量"的约束;定位段因此可以合法地把任务预算花光而一次补丁都不提。
可执行的改动方向(属禁区语义,须先讨论):给 PROPOSE 段预留固定比例预算,或给 LOCALIZE 设
"读满 N 轮必须给结论"的软收束。

## 本档四道真问题的账目(全程真实花费)

| 阶段 | tokens |
| --- | --- |
| 第一档消融臂 7 题(handicap 版,作废) | 777k |
| 第二档真实臂 3 题 | 1,033k |
| 第二档补齐两臂 3 次运行 | 953k |
| 合计 | ≈2.76M |

买到的可复用产出:1 例真实多文件题在平台判定下拉通(见上表 astropy/xarray)、
3 条设计/实现缺陷(`reasoning_content` 未回传已修 `4a4093e`、消融臂 handicap 已撤 `f4a128e`、
定位段预算无预留)、1 条协议边界(块协议无法表示上下文重复的金补丁)。

## 复现

```text
# 第一档(最易 5 题,默认口径 12 轮 / 200k / 900s)
PATCHPILOT_EXECUTION_BACKEND=docker python -m app.evals.run_single \
  --bug SWE-pallets__flask-5014 --model openai --engine plain --arm one_shot \
  --max-turns 12 --out runs/swe-oneshot-1

# 第二档(难题集,两臂同口径:24 轮/段、400k、1800s)
PATCHPILOT_EXECUTION_BACKEND=docker PATCHPILOT_TOKEN_BUDGET=400000 \
PATCHPILOT_TASK_TIMEOUT_SECONDS=1800 python -m app.evals.run_single \
  --bug SWE-astropy__astropy-8707 --model openai --engine graph --max-turns 24 \
  --out runs/swe-hard-graph
# 消融臂同题同参,只把 --engine graph 换成 --engine plain --arm one_shot --out runs/swe-hard-oneshot2

# 两臂对照(三段口径缺一不可)
python -c "from app.evals.metrics import annotate, compute_metrics, load_run; \
from pathlib import Path; \
print(compute_metrics([annotate(load_run(p), Path('bugs')) \
for p in sorted(Path('runs/swe-oneshot-1').glob('SWE-*'))]))"
```

离线自检:`pytest tests/test_single_shot.py`(7 例,零网络;含"补丁阶段一次 run_tests 都没真执行"的不变量)。
