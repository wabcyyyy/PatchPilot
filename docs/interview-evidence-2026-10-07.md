# 简历与面试:能写什么、不能写什么(2026-10-07 实测口径)

这份文档的唯一目的:让我写下的每一句都能被仓库里的产物核对。
左列是句子,右列是"追问时打开哪个文件"。

## 可以写(附证据与强度)

| 主张 | 证据 | 强度 |
| --- | --- | --- |
| 平台在题目自带的时代正确环境里独立执行测试再判定,**不采信模型自述** | `runs/swe-real/*/report.json`(5 题 `verify_failed_ok`/`verify_regression_ok` 全 True,而模型侧全部只声明 `finish(success=true)`) | 强:逐题产物可查 |
| 块协议单入口在 **5 个真实第三方仓库**上零门禁绕过 | 同上 `gate_violations: []`;`tests/test_attacks.py`、`tests/test_blockpatch.py` 全绿 | 强 |
| 我给项目建了**消融对照臂**,并用它**证伪了自己的头条结论** | `app/evals/single_shot.py` + `docs/swe-ablation-evidence-2026-10-07.md` 第 1 节:同一批真实题,去掉测试反馈与重试后**仍 5/5**,token 只要一半(54,098 vs 100,738) | 强:这是我最想被追问的一条 |
| 对照臂与真实臂**共用同一条判定路径**(基线/验证/门禁/判定不分叉) | `run_task` 的 `agent` 执行体插槽(`app/evals/driver.py`);不变量钉在 `tests/test_single_shot.py` | 强 |
| 定位并修复两个**只在真实运行里才暴露**的缺陷 | ① 思考模式 `reasoning_content` 未回传 → 多轮请求 400(`4a4093e`,三例零网络用例);② 我给对照臂私加 6 轮上限 → 造成 0/7 空结果(`f4a128e`) | 强:有 commit、有用例 |
| 找到这一档难度的**真瓶颈**,并给出代码位置 | `app/graph/nodes.py::_token_budget_for` 把整份任务预算交给定位段,无"给补丁段留量"约束 → 真实多文件题可以"只读调查花光 400k、一次补丁不提"(`runs/swe-hard-graph/SWE-sphinx-*/report.json` 的 `BUDGET_EXCEEDED`) | 中-强:机制清楚,样本小 |
| 查出块协议的一条硬边界 | 上下文重复的上游金补丁无法协议 round-trip(见 `docs/swe-ablation-evidence-2026-10-07.md` "顺带查出的块协议真实边界");样本隔离在 `runs/swe-hard-2026-10-07/quarantine/` | 强:可复现 |
| 工程规模与质量:**515 passed / 2 skipped**,评测每个数字可复现 | `ruff check . && pytest -q`;批次报告自动归并复现命令(`app/evals/report.py`) | 强 |

## 不能写(以及为什么)

- **~~"SWE-bench Verified 解决率 5/5 / 100%"~~**
  样本是我挑的最易档(100% 属官方 `<15 min`、全单文件、补丁 14–20 行;数据集里 54% 补丁更大、14% 多文件),
  n=5 的 95% 置信下界只有约 55%;且判定规则与提交形态是自定的,不是官方 harness。
- **~~"agent 循环带来了净增益"~~**
  两臂在最易档同为 5/5;难题档只有 2 对可比,真实臂 2/2 vs 消融臂 1/2,差值 1,
  而**预先登记的判据**写的是"差 ≤1 ⇒ 无净增贡献"。唯一那处差异还能用定位段的采样随机性解释
  (同段同提示同工具:11 轮 vs 19 轮),每臂 n=1 分辨不了机制与运气。
- **~~"修出来的补丁等同于上游修法"~~**
  反例是自己查出来的:`pylint-6903` 上消融臂给了不同修法(把可用 CPU 为 0 变成 `None` 而不是兜到 1),
  F2P+P2P 照样全绿、判 resolved。判定的天花板是"声明的测试集全绿 + 范围合法"。
- **任何金额表述**:`cost_usd` 恒为 `null`(价目表未收录该模型),只有 token 数。
- **"复刻了 SWE-agent / SWE-bench"**:借的是设计与接口,README 第 11 行一直明确不这么声称。

## 三个最可能的追问,以及一句话答法

1. **"两臂都 5/5,那你这个项目还有什么?"**
   → 我的成绩不是解决率,是**可核验性**:测试在题目自己的历史环境里真跑、门禁一次都没被绕过、
   判定不采信模型自述、每个批次自动带 provenance 与可执行复现命令——这些正是让"5/5"这种数字
   值得被相信的部分;而消融是我自己设计来做证伪它的。
2. **"你为什么主动报告坏消息?"**
   → 因为可写的主张必须能被产物核对;证伪掉的那句(循环有净增益)一旦被写进简历,
   面试官追问两层就塌,而"我建了消融臂并让它推翻我的头条"这句可以无限追问下去。
3. **"下一步做什么?"**
   → 先把预算语义改对(给补丁段预留量 / 定位段软收束),再把对照扩到 ≥4 对 ×2 重复;
   在那之前不宣称循环有净增益。

## 复现

```bash
# 消融臂(真实模型,题目需已导入且镜像在本地)
PATCHPILOT_EXECUTION_BACKEND=docker python -m app.evals.run_single \
  --bug SWE-pallets__flask-5014 --model openai --engine plain --arm one_shot \
  --max-turns 12 --out runs/swe-oneshot-1

# 离线自检(零网络,含"臂内一次 run_tests 都没真执行"的不变量)
pytest tests/test_single_shot.py
```

数据目录:`runs/swe-real`(真实臂,最易档)、`runs/swe-oneshot-1`(消融臂,最易档)、
`runs/swe-hard-graph`(真实臂,难题档)、`runs/swe-hard-oneshot`/`runs/swe-hard-oneshot2`
(消融臂,难题档,前者是我作废的 handicap 版)。
