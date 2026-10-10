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

