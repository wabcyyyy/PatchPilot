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
| 补丁阶段 | `run_tests` 可用,VERIFY 失败可回滚重试(最多 5 轮) | **无 `run_tests`**,单发即终局(≤6 次工具调用) |
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

## 复现

```text
# 消融臂单题(真实模型;容器镜像需已导入,后端必须 docker)
PATCHPILOT_EXECUTION_BACKEND=docker python -m app.evals.run_single \
  --bug SWE-pallets__flask-5014 --model openai --engine plain --arm one_shot \
  --max-turns 12 --out runs/swe-oneshot-1

# 两臂对照(三段口径缺一不可)
python -c "from app.evals.metrics import annotate, compute_metrics, load_run; \
from pathlib import Path; \
print(compute_metrics([annotate(load_run(p), Path('bugs')) \
for p in sorted(Path('runs/swe-oneshot-1').glob('SWE-*'))]))"
```

离线自检:`pytest tests/test_single_shot.py`(7 例,零网络;含"补丁阶段一次 run_tests 都没真执行"的不变量)。
