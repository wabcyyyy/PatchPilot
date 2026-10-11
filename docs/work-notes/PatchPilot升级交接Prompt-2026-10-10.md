# PatchPilot 升级：新对话交接提示

初版日期：2026-10-10。执行约定修订：2026-10-11。完整任务为 G00–G17；实施状态以 [执行进度](agent-upgrade-progress-2026-10-10.md) 为准。下方 B0 内容是初始启动模板，已执行卡不重新启动。

在实际项目 `D:/wcy/project/PatchPilot` 打开新对话并执行。历史评审基线来自另一工作树，G00 核验已记入进度；先核对当前 HEAD、dirty 文件与适用收据，不能直接沿用历史缺陷或测试结论。

2026-10-11 起，开发验证统一按 `AGENTS.md` 的“开发验证节奏”执行；每卡定向检查后提交，
批次退出才做全量。下方 B0 提示保留初始交接背景；后续执行以当前进度与适用收据为准。
新对话若从批次中途继续，不重复 G00 或已完成卡的验证。

## 初始 B0 启动模板（已执行时从进度继续）

```text
请直接实施 PatchPilot 修复 Agent 升级的第一批 B0（G00–G01），不要只返回计划。

产品定位：专注仓库代码修复的精简 coding agent。保留严格验收、隔离执行、冻结输入、候选 hash 和审计；逐步补齐连续上下文、受控执行、用户交互与有限 subagent。当前这一批只做基线核验、PLAN 完成协议和可见工具权限。

先完整阅读：
1. D:/wcy/project/PatchPilot/AGENTS.md
2. D:/wcy/project/PatchPilot/docs/PatchPilot修复Agent升级SPEC-2026-10-10.md
3. D:/wcy/project/PatchPilot/docs/work-notes/agent-readiness-review-2026-10-10.md
4. SPEC 指向的当前相关源码、测试，以及 docs/adr/0007-计划工件-plan-成为图上的节点.md。

执行目录：D:/wcy/project/PatchPilot。
同步时实际项目 HEAD：876ff6fedd0a2e04b04bb489e05e2073ab5ea485，分支 master；历史评审基线另见评审正文。
这些是交接时事实，开始必须重新检查 git status、HEAD、branch、解释器和依赖，不照搬旧结果。
detached HEAD 时创建 codex/repair-agent-b0；若已有合适分支则复用，不 reset、不覆盖用户变更。

需保留的未跟踪文档/证据：
- docs/work-notes/agent-readiness-review-2026-10-10.md
- docs/work-notes/audit_agent_review_20261010.py
- docs/work-notes/2026-10-10-agent-review-probes.json
- docs/PatchPilot修复Agent升级SPEC-2026-10-10.md
- docs/work-notes/PatchPilot升级交接Prompt-2026-10-10.md
它们是既有审查/SPEC 成果，不是本批实现结果。其他新增变更也先识别并保留。

G00：建立 docs/evidence/agent-upgrade/baseline.json 与 docs/work-notes/agent-upgrade-progress-2026-10-10.md。
核对旧 S00–S11 已落地能力，避免重复整改；核对当前 PLAN 和 schema 缺陷是否仍存在。
运行离线基线检查；产品失败、环境失败和未运行分开记录。

G01：
- PLAN 使用明确的单次文本完成契约。正常、非空、非截断纯文本计划应一次请求完成，不能继续循环直到 phase_share/max_turns。
- legacy 文本+finish 保持兼容，但计划阶段不能执行读写/测试等真实工具。
- 空回复、截断和非法工具最多一次协议修正，仍失败则结构化降级并记录，禁止扩大份额掩盖问题。
- 模型可见 schema 只包含当前阶段/实验臂允许工具；执行层再次校验同一能力集合。
- FakeLLM 保留旧兼容模式，新增严格脚本模式测试普通模型响应；不能用 marker 自动补 finish 来证明契约成立。
- 保留 one_shot 的禁反馈/禁分支约束；模型 finish 永远不等于平台 resolved。

允许修改范围以 SPEC G00/G01 为准。G01 的业务范围：
app/prompts.py、app/graph/nodes.py、app/graph/plain_loop.py、app/tools/registry.py、app/llm/fake.py、app/evals/experiment_policy.py。
app/llm/openai_client.py 只允许空 tools 请求兼容接线，不做 G02 的计量或重试重构。
测试范围：tests/test_plan_stage.py、test_plan_invariants.py、test_plain_loop.py、test_graph.py（必要契约覆盖）、test_llm.py，以及新增 test_tool_capabilities.py。
另可更新对应 ADR、升级进度和本批证据。发现依赖确需越界时先记录范围扩张及理由；不要借此启动后续大重构。

本提示明确授权按上述规格修复 app/graph 等必须掌握区内的协议语义，不需要再次询问同一事项。
禁止修改既有测试期望来让实现通过、降低门禁或测试身份规则；旧协议断言保留 legacy 覆盖，新协议另加测试。
禁止提前做 G02–G17；不改窗口、总预算、阶段份额、价格、默认执行后端和实验参数。
不运行真实模型、在线探针或 paid batch，不 push、不发布、不删除历史结果，不修改用户源仓库。
单次生成/补丁块不超过 300 行；每张卡至少一个 Conventional Commit，提交前快速检查与相关定向测试通过。
开发验证按 AGENTS.md 执行；卡内记录“定向通过，批次全量待运行”，不因新开对话重复基线或全量。

实际项目解释器（启动前复查）：D:/wcy/project/PatchPilot/.venv/Scripts/python.exe，Python 3.14.2。
先复查；源码、数据及执行目录都使用 D:/wcy/project/PatchPilot。
pytest basetemp 必须在仓库外且可写。本交接会话外部可写示例：
D:/tmp/pt-upgrade-b0
新对话权限若不同，选择它实际可写的外部落点并记录。不要放回仓库内。

PowerShell 每卡快速检查与 B0 定向测试示例，逐条检查退出码，失败先修复：
$taskPython = 'D:/wcy/project/PatchPilot/.venv/Scripts/python.exe'
$taskBaseTemp = 'D:/tmp/pt-upgrade-b0'
& $taskPython -m ruff check .
& $taskPython -m ruff format --check .
& $taskPython -m pytest -q tests/test_docs_anchors.py --basetemp=$taskBaseTemp
& $taskPython -m pytest -q tests/test_plan_stage.py tests/test_plan_invariants.py tests/test_plain_loop.py tests/test_graph.py --basetemp=$taskBaseTemp
批次全部实现与文档准备好后，再执行一次最终全量并保存日志、退出码：
& $taskPython -m pytest -q --durations=30 --basetemp=$taskBaseTemp
全量期间保持被测文件不变，不另起 pytest。失败先单独复现/修复并定向补验，再做必要的最终全量。
通过后只更新进度文字/收据时，按影响检查文档即可；重复或扩大验证须说明具体变化或风险。
本批新测试也必须包含在最终全量检查中；不要将上一轮 54 个测试通过当本批验证。

最终交付：
1. G00/G01 的修改、语义、commit 和文件范围。
2. 正常纯文本计划一次完成、非法工具零副作用、schema 与实际权限一致的直接测试证据。
3. ruff/format/锚点/目标测试及批次全量收据，环境限制与未运行项；中途交接明确全量待运行。
4. 新增的 baseline/进度/证据路径。
5. 明确说明付费调用和外部动作是否为零，以及剩余缺陷。
6. 给下一批 B1（G02–G04）一份带当前 HEAD/dirty 文件/命令的交接，不自行启动付费或后续批。
```

## 后续批次的通用交接模板

以下模板用于后续批次或批次中途接续；填入最新收据、当前卡和待验证项，不引用初始 HEAD 作为当前身份。未完成批次先完成当前授权范围，不因换对话越过批次退出条件。

```text
请实施 docs/PatchPilot修复Agent升级SPEC-2026-10-10.md 中的 [批次名称与卡号]。
先读 AGENTS.md、完整 SPEC、docs/work-notes/agent-upgrade-progress-2026-10-10.md 和上一批证据。
当前工作树：[实际绝对路径]；HEAD：[最新值]；需保留的 dirty 文件：[清单]。
上一批：[卡号、commit、检查结果、仍未验证事项]。
本批允许范围/行为/验收严格按对应卡实施，发现已实现能力先核验，不重复重写。
按规格讨论过的语义直接实现，不降低门禁、测试身份和独立验收。
默认仅离线 FakeLLM/mock；G17 真实调用、push、发布等需要另有明确授权。
开发验证按 AGENTS.md 的“开发验证节奏”执行：每卡快速检查、必要行为测试与直接受影响回归通过后提交，更新进度/证据。
卡内全量状态登记为待运行；批次退出时统一跑一次完整 pytest 并记录耗时分布，缺全量通过收据不宣称批次完成。
失败先定向修复/补验；重复或扩大检查前说明具体变化或风险。先查锚点再跑耗时测试，pytest 串行，全量期间保持被测文件不变。
跨对话先读适用收据，继续当前批次，不重跑已完成卡；产品失败、环境失败与未运行分开报告。
完成后交付修改、commit、验证、限制、外部成本，并为下一批生成一份具体交接。
```

完整升级顺序：B0→B1→B2→B3→B4→B5→B6。用户仅说“继续 SPEC/当前批次”时，依据进度定位当前卡或下一批，不重新套用 B0 模板。G15 是可选扩展，G17 是另行授权的在线证据工作。不要把“文档已有任务卡”写成“功能已实现”。
