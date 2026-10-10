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
