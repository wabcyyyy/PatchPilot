# S00 实施基线清单(spec:PatchPilot执行SPEC-2026-10-08)

日期:2026-10-08。本清单记录执行 spec 前的代码、环境与回放基线;全部数字为本机实测,
不是沿用历史文档。

## 0. 执行根决策(与 spec §1 的偏离声明)

spec 书写的执行根是 `C:/Users/25924/.codex/worktrees/a4d3/PatchPilot`(spec 所在的 codex
worktree,detached HEAD)。本次实施在 **`D:/wcy/project/PatchPilot`**(用户会话工作目录、
`.venv` 与历史 `runs/` 所在地)进行,依据 spec"在其他 checkout 执行时必须先确认相同代码身份":

- 两个 checkout 的 HEAD 逐字相同:`442cd8db3efe45deadbaf185ef18e42e3595a028`;
- D 侧工作树 clean(spec 是 C 侧唯一未跟踪文件,已复制入本 checkout 一并入库);
- 新建分支 `codex/patchpilot-reliability-20261008` 承载全部实施 commit,master 不动;
  两 checkout 共享同一 git 对象库,分支在两侧均可见。
- C 侧 worktree 本轮零写入。

## 1. 环境

| 项 | 值 |
|---|---|
| 解释器 | `D:/wcy/project/PatchPilot/.venv/Scripts/python.exe`,实测 3.14.2(spec §2 已声明;目标仍为 3.11+,未在 3.11 上验证不写成已验证) |
| ruff | 0.16.7(check 与 format --check 均通过:193 files) |
| 环境变量 | `PATCHPILOT_LLM_ENABLED=false`(所有 pytest 与批运行) |
| pytest basetemp | `D:/tmp/pt-patchpilot-spec-20261008`(仓库外;`D:/wcy/project/PatchPilot` 不在其父链上) |
| 收集数 | **722 collected**(审查材料记 710;两侧同 commit,差异未逐例归因,以本侧 collect-only 实测为准) |

## 2. 全量离线 pytest 基线

命令与退出码(依次执行,未并发):

```
PATCHPILOT_LLM_ENABLED=false .venv/Scripts/python.exe -m pytest -q \
  --basetemp=D:/tmp/pt-patchpilot-spec-20261008        # 退出码 0(见下)
```

- 首跑结果(加入 ADR-0009 之后):**2 failed, 717 passed, 3 skipped**,1108.51s;
  3 条警告(Starlette/AnyIO 弃用、local_runner dataclass 收集提示、requests 语料转义),
  与审查记录一致。
- 2 个失败均为 `tests/test_docs_anchors.py` 对**本次新入库的 ADR-0009**的锚点格式断言
  (验证锚点节未按 `- \`路径:行号\` — \`期望子串\`` 格式书写),不是产品代码失败;
  修正锚点格式后复跑该文件:6 passed。产品用例零失败。
- skip 明细(环境性,如实记录):redis×2(`redis://localhost:6379/0 无可用 Redis 服务`)、
  符号链接×1(Windows 未授创建权限)。

结论:基线绿(产品 717 passed + 3 环境性 skip);文档锚点断言失败由本卡新文档引致并当卡修复,
不构成"基线红被掩盖"。

## 3. FakeLLM 回放基线批(spec §6 S00 命令)

预期对照:历史批 `runs/fake35-v2`(plain,35/35 resolved)与 `runs/graph35-v2`
(graph,35/35 resolved)。本轮生成**新目录**,不覆盖历史产物:

| 批 | 命令 | 结果 |
|---|---|---|
| plain | `python -m app.evals.driver --bugs all --model fake --engine plain --out runs/spec-baseline-plain-20261008` | 35 tasks, verdicts={'resolved': 35},退出码 0;manifest:commit `442cd8db3efe`,worktree_dirty=false |
| graph | 逐题 `python -m app.evals.run_single --bug BUG-xxx --model fake --engine graph --out runs/spec-baseline-graph-20261008`(35 次) | 35/35 退出码 0;逐题 report.json 复核 verdict 全为 resolved、bug_id 去重 35 |

graph 批 CLI 不存在(driver 仅支持 plain),按 spec 逐题调用 run_single;
run_single 退出码 0 仅代表该题 resolved,逐题 rc 已核对:35/35 为 0。

## 3.1 批次复核

- plain 批:`batch_manifest.json` 的 `git_commit=442cd8db3efe…`、`worktree_dirty=false`、
  `input_anchor_checked=true` 且无 missing;
- graph 批:无批次 manifest(逐题 CLI 的已知边界),以 35 份 `report.json` 复核——
  verdict 全为 `resolved`,bug_id 去重 35,覆盖 BUG-001..BUG-035 全集。

<!-- S00-GRAPH-BATCH-RESULT -->

## 4. 已确认缺陷(本 spec 的修复对象,基线时点全部在案)

F1 末回复超预算仍 resolved(P1)/ F2 next=finish 恢复清补丁仍 resolved(P0)/
F3 测试身份漏类链(P0)/ F4 custom ID 无字段边界+不可重建(P1)/ F5 门禁拒后轮次被跳(P1)/
F6 空 touched 算 strict 成功(P1)/ F7 演示传旧 diff_text(P1)/ F8 消融两臂不同引擎(P1)。
复现边界与修复顺序见 spec §4/§5;契约决定见 ADR-0009。
