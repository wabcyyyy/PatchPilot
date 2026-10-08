# 简历与面试:能写什么、不能写什么(2026-10-07 立稿,**2026-10-08 复核并补架构升级段**)

这份文档的唯一目的:让我写下的每一句都能被仓库里的产物核对。
左列是句子,右列是"追问时打开哪个文件"。
2026-10-08 的复核动作:`scripts/compare_batches.py` 两臂各重跑一次(判定字段逐题相等)、
真实模型成本逐份 `report.json` 重新加总、全量用例数重新实测 —— 不是照抄昨天的数字。

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
| **架构升级不破行为**:夜间上下文/检索/plan/恢复七卡落地后,两臂零成本回放与升级前基线**逐题判定字段全等** | `scripts/compare_batches.py runs/graph35-v2 runs/night-final-2026-10-08`(35 题 9 字段 0 差异,成本 turns 249→284、tokens 6,917→7,445,差值就是 PLAN 那一次调用)与 `... runs/fake35-v2 runs/night-plain-final-2026-10-08`(plain 臂 turns 214→214、tokens 6,225→6,225 一字未动) —— 2026-10-08 各重跑复核过 | 强:一条命令可复现,判定字段不等就退出码 1 |
| 检索升级用**真实上游仓库**复查,不是单测形状 | `scripts/measure_retrieval_domain.py`:12 题真实仓库、20 个必改文件在新遍历域内全部可达(全域最大位次 1168),其中 **5 个文件在旧的 500 条上限下根本不可见**;AST 大纲 20/20 可解析、给出 2,263 个符号 | 强:零成本、当场可跑 |
| 崩溃恢复拿到**真·进程死亡**证据,不是异常模拟 | `tests/test_resume_crash.py`:子进程 `os._exit(7)`(不走 finally、不关检查点连接)后由另一进程续跑,钉住"工作区复位 + 门禁与双测试集重跑 + 已完成阶段不重放" | 强 |
| **发现自己写的测试是假通过**,并把它变成机制防线 | `test_context_wiring.py` 压缩用例当时靠"basetemp 恰好在仓库内"才绿(`.git` 父链);改成真 git 工作区 + `patch_applied is False` 断言,并在 `tests/conftest.py` 装 session 级防线:落点进了仓库就**当场整体报错** | 强:这条我最愿意讲 |
| 把"并发执行互删临时根"从推测测成数字,再修掉 | `scripts/measure_basetemp_contention.py`:共用 report_dir 的并发执行 **9/18 伪失败**(隔离组 0/18,控制组单独跑全过);修复 `a127b28` 后 0 次,`tests/test_basetemp_isolation.py` 守住,**反向验证**过(把落键改回旧写法该用例 12/18 红) | 强:失败形态三种都抓到了 |
| 我装的一条拦截,把文档里的默认命令冲掉了 —— **我自己发现并收口** | `pyproject.toml` 的 `--basetemp=.pytest-tmp`(仓库内)与 M10.5 防线相冲 ⇒ `AGENTS.md`/`README` 写的 `pytest -q` 必然失败;`94d558b` 把落点移出共享配置、文档同步改跑法,连带修掉 `measure_retrieval_domain.py` 的"报 0 题"假干净(`1e65a80`) | 强:有 commit,讲得出取舍 |
| 工程规模与质量:**708 passed / 3 skipped**,评测每个数字可复现 | `ruff check .` 全绿 + `pytest -q --basetemp=D:/tmp/pt`(2026-10-08 实测 1229s);批次报告自动归并复现命令(`app/evals/report.py`) | 强 |


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
- **金额可以写了,但只能这样写**:真实模型批次共 **25 份 `report.json`,24 份带 `cost_usd`,
  合计 4,336,298 tokens ≈ $1.46**。两个必须一起说的限定:①`deepseek-flash` 官方价分 peak/off-peak,
  价目表按 **peak 高档估算**(`app/evals/pricing.py:18`),不是账单;②少的那 1 份是崩溃路径
  把 turns/tokens 记成 0 的 `NEEDS_REVIEW`(`runs/swe-hard-oneshot/SWE-sphinx-doc__sphinx-7748-*/report.json`),
  正是 `4a4093e` 修的那个缺陷的实物证据 —— "花了钱没入账"我自己留了痕。
- **架构升级各机制对修复率的作用一律未证明**:滑动窗口压缩(阈值 16000)、仓库骨架持久记忆、
  PLAN 段、失败反思、遍历域解耦 —— 零成本回放只证明**接线不破行为**(FakeLLM 无视消息内容,
  ADR-0003 自认),要主张"提升修复率/token 效率"必须真实模型批,还没跑。
  能写的只有上一条那组"判定字段逐题相等"与"真实仓库位次"这种**结构性证据**。
- **"CI 已经绿了"**:CI 跑 `pytest -q`(`.github/workflows/ci.yml:26`),我把 addopts 里仓库内的
  落点去掉后它与防线不再相冲(这是配置层推理),但**我没在 CI 上真跑过**,不宣称 CI 绿。
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
# 离线自检(零网络;落点必须在仓库外,本机系统临时目录的 pytest 根 ACL 已坏 —— PM-004)
pytest -q --basetemp=D:/tmp/pt

# 两臂零成本对照:判定字段逐题相等,不等即退出码 1(成本字段只报差异)
python scripts/compare_batches.py runs/graph35-v2 runs/night-final-2026-10-08
python scripts/compare_batches.py runs/fake35-v2 runs/night-plain-final-2026-10-08

# 真实上游仓库的检索遍历域复查(需要 base-SWE-* 缓存在场,落点用 --root= 指)
PYTHONPATH=. python scripts/measure_retrieval_domain.py --root=D:/tmp/pt

# 并发临时根的伪失败测量(零成本、不起模型)
PYTHONPATH=. python scripts/measure_basetemp_contention.py

# 消融臂(真实模型,题目需已导入且镜像在本地)
PATCHPILOT_EXECUTION_BACKEND=docker python -m app.evals.run_single \
  --bug SWE-pallets__flask-5014 --model openai --engine plain --arm one_shot \
  --max-turns 12 --out runs/swe-oneshot-1

# 臂内不变量(一次 run_tests 都没真执行)
pytest tests/test_single_shot.py
```

数据目录:`runs/swe-real`(真实臂,最易档)、`runs/swe-oneshot-1`(消融臂,最易档)、
`runs/swe-hard-graph`(真实臂,难题档)、`runs/swe-hard-oneshot`/`runs/swe-hard-oneshot2`
(消融臂,难题档,前者是我作废的 handicap 版)。

---

# 对外文案(可直接抄,每条都能被仓库产物核对)

## 项目一句话

PatchPilot:给定本地 Git 仓库 + Bug 描述,Agent 检索代码、按块协议提交补丁、在隔离工作区执行
预定义测试集,由平台按四条件判定(原失败测试转绿 ∧ 回归集全绿 ∧ 通过全部门禁 ∧ 未超资源预算),
**不采信模型自述**。自建 35 题评测集 + SWE-bench Verified 真实题接入;当前 708 个 pytest 用例
(3 skipped 是 Redis/符号链接权限这类环境依赖)、零网络测试套件,每条评测数字都有落盘产物。

## 简历成就句(6 条,按可核对性排过)

1. **设计并落地"补丁块协议"单入口**:模型只能提交 `*** Begin/End Patch` 块格式,unified diff 直接拒;
   上下文行唯一性、软链(模式 120000)一律拒、路径越界、测试文件改动等构成可复核的拒因链,
   并由 round-trip 语料测试长期守住不变量(含"协议产物永不含软链")。
   证据:`app/gitops/blockpatch.py`、`tests/test_blockpatch.py`、`tests/test_attacks.py`。
2. **把外部真实题接进同一套判定**:SWE-bench Verified 元数据 → 本地任务,每题自带时代正确的
   容器执行环境(`env: python/image/workdir/network`),导入期从官方镜像回捞构建产物;
   **基线可证是入库硬条件**——没有绿灯探针(P2P 为空)的题直接拒收。
   证据:`scripts/import_swebench.py`、`app/adapters/pytest_adapter.py::BugEnv`、`docs/swe-ablation-evidence-2026-10-07.md` 的导入账目。
3. **为"Agent 循环到底值不值"实现消融对照臂**,并强制两臂共用同一条判定路径(只在执行体上开插槽);
   **它推翻了我自己的头条结论**:最易 5 题两臂同为 5/5,消融臂 token 只要一半(54,098 vs 100,738)。
   证据:`app/evals/single_shot.py`、`runs/swe-oneshot-1`、`docs/swe-ablation-evidence-2026-10-07.md`。
4. **修掉两个只在真实模型运行下才暴露的缺陷**:① 思考模式的 `reasoning_content` 未回传 → 多轮请求
   直接 400,且崩溃路径连 token 用量都不入账(`4a4093e`);② 定位段可独占整份任务预算 → 真实多文件题
   "读满预算、一次补丁不提"就被判死,改为按份额取预算 + 额度耗尽带暂定结论降级继续(`157c7c7`、`293cd64`)。
5. **评测可复现性做成产物而不是口头**:`report.json` 自带 provenance(git commit / 工作树脏状态与指纹 /
   配置快照 / 模型身份 / 执行后端),批次报告自动归并**可执行**复现命令;信息缺失时写"无法判定",
   不编造能跑的假命令。证据:`app/evals/provenance.py`、`app/evals/report.py`、`tests/test_driver.py`。
6. **把实验的代价与失败一起公开**:真实模型开销 25 份落盘报告、4,336,298 tokens、按 peak 档价目
   表估算约 $1.46(限定条件见"不能写"一节,含那份"花了钱没入账"的崩溃报告);题单与判据**先入 git 再跑**
   (防事后挑样本);分母不缩(预登记 10 题→8 可证→7 金补丁可修→2 对可比),不达标的结论照写。
7. **做了一次上下文与检索的架构升级,并用零成本回放证明它不破行为**:滑动窗口机械压缩、仓库骨架
   持久记忆、AST 结构感知检索(rg 只做文件级预筛 + 符号/文件描述工具,遍历域与输出体量分设上限)、
   失败反思带上被丢弃补丁的形状摘要、`LOCALIZE→PLAN→ACT→VERIFY` 与两级崩溃恢复。
   落地后 graph 与 plain 两臂各 35 题重放,与升级前基线**逐题判定字段全等**,成本差值恰好等于 PLAN
   那一次调用(turns 249→284);plain 臂 turns/tokens 一字未动。
   证据:`scripts/compare_batches.py`(判定字段不等即退出码 1)、`runs/night-final-2026-10-08`、
   `runs/night-plain-final-2026-10-08`、`PROGRESS.md` 的 M1-M10 卡。**明确不主张这些机制提升了修复率。**
8. **检索升级是拿真实上游仓库复查的,不是单测形状**:12 题真实仓库里 20 个必改文件在新遍历域全部可达
   (最大位次 1168),其中 **5 个在旧的 500 条上限下根本不可见** —— 也就是那条缺陷真实挡住过题;
   AST 大纲在第三方代码上 20/20 可解析。证据:`scripts/measure_retrieval_domain.py`、
   `docs/adr/0005-检索引擎-rg只做文件级预筛.md`。

## 60 秒口径(面试自述)

"我做的是给真实仓库打补丁的验证平台,不是又一个聊天 Agent。它的核心主张是**判定权不归模型**:
模型只能用块协议提交补丁,平台在它自己的隔离工作区、用题目自带的历史正确环境跑测试,四个条件全满足
才算修好。为了让这些数字值得相信,我接了 SWE-bench Verified 的真实题、给每题做了容器执行环境、
每份报告都带可复现的 provenance。更重要的是我给自己做了一个消融臂——把测试反馈和重试都拿掉,
判定路径一字不改地复用同一条。结果它把我的头条结论证伪了:最易那批 5 题两臂都是 5/5,而消融臂只花一半
token,所以我不写'循环提升了修复率',我写'循环的价值在这批样本上未被证明',同时把为什么没测出来定位到了
具体函数——定位段能独占整份预算。过程中还修了两个只有真实调用才会撞到的缺陷。"

## 英文版(2 条)

- Built a patch-**verification** platform (not another chat agent): the model submits patches only via an
  apply_patch block protocol; the platform runs the task's predefined test sets in an isolated workspace
  inside the instance's era-correct container, and resolves a task only when all four conditions hold —
  it never trusts the model's own claim.
- Shipped an ablation arm that removes execution feedback and retry while reusing the **same** judging path,
  and it falsified my headline claim: both arms resolved 5/5 on the easy tier at half the tokens; I report the
  loop's value as unproven and trace the binding constraint to the localize phase's budget handling.

## 追问三层时怎么接

| 追问 | 答法 |
| --- | --- |
| "两臂都 5/5,那你平台提供了什么?" | 提供的是**可核验性**:环境对代、测试真跑、门禁零绕过、判定不采信自述、provenance 可复现。5/5 之所以敢报,是因为有消融臂;不是因为有 5/5。 |
| "难题档跑成什么样?" | 预登记 10 题→8 可证→7 金补丁可修→只有 2 对可比(真实臂 2/2、消融臂 1/2,差 1,按事前判据等于没证明)。唯一那处差异还能用定位段采样随机性解释,每臂 n=1,不算机制证据。 |
| "那你怎么收尾?" | 同一道 107 行/3 文件的题独立跑两次,两次都是"读到预算耗尽也没提交补丁"。我按事先写死的停手条件停了付费(累计约 4.34M tokens / 估算 $1.46),把它写成负面结论,而不是抬预算赌一次。 |
| "你凭什么让人信你的结论不是实验设计造出来的?" | 三条纪律都留了痕:①判据与题单**先入 git 再跑**;②消融变量只允许登记过的那两个,我给自己对照臂私加 6 轮上限那次,结果整批 0/7 作废并写进文档(`f4a128e`);③新写的回归用例必须**反向验证**——我把修复改回旧写法让它在同一命令下 12/18 红,否则那条用例只是装饰物。 |

**仍然不能对外说的话**(见上文"不能写"一节):任何解决率/跑分表述、"循环带来净增益"、
"补丁等同上游修法"、以及把架构升级的各机制(压缩阈值 / 仓库骨架 / PLAN 段 / 反思 / 遍历域解耦)
说成"提升了修复率"——它们只证明了接线不破行为。金额可以写,但必须带"按 peak 档估算、非账单"
与"24/25 份报告有成本字段"两个限定。

