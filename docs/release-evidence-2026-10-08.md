# Release Evidence(2026-10-08 spec 执行收口)

本文件是《PatchPilot执行SPEC-2026-10-08》S00–S11 的证据索引。全部数字为最终
HEAD 上的实测,不是沿用历史文档。能力声明分四级:**implemented**(代码在库)、
**offline verified**(离线测试/回放实测)、**live verified**(真实模型在线实测,
本轮为零)、**planned**(仅有设计/预登记)。

## 1. 代码身份与环境

| 项 | 值 |
|---|---|
| 最终 HEAD | 见 `git rev-parse HEAD`(分支 `codex/patchpilot-reliability-20261008`,基线 `442cd8d`) |
| 提交序列 | 9e8aab8(S00)→ 28f97f0(S01)→ b1b8865(S02a)→ e78ea14(S02b)→ 6c47e92(S05a)→ 34fd5ac(S03)→ 25b8de7(S04)→ 110369d(S05b)→ 24568ba(S06)→ 5f0d615(S07)→ efcf016(S08)→ b33261f(S09)→ c9a07a8(S10a/b)→ S11(本文件) |
| 解释器 | `.venv` Python 3.14.2(目标 3.11+,**未在 3.11 实测**,如实声明) |
| ruff | 0.16.7,`ruff check .` 与 `ruff format --check .` 全绿(217 files) |
| 全量离线测试 | `PATCHPILOT_LLM_ENABLED=false python -m pytest -q --basetemp=D:/tmp/pt-patchpilot-spec-20261008` → **869 passed, 4 skipped**(skips:redis×2、Windows 符号链接×2,全部环境性),退出码 0,最终闸实测约 22–25 分钟 |
| LLM | 全程 `PATCHPILOT_LLM_ENABLED=false`,FakeLLM 回放;**本轮零真实模型调用** |

## 2. 缺陷修复证据(F1–F8 全部关闭)

| 编号 | 缺陷 | 修复 commit | 回归测试(反转证据) |
|---|---|---|---|
| F1(P1) | 末回复超预算仍 resolved | b1b8865 | `tests/test_f1_budget.py`:graph/plain 末回复 50000→BUDGET_EXCEEDED 且用量入账;多工具超额回复工具不执行;超额 finish 不兑现 |
| F2(P0) | next=finish 恢复清补丁仍 resolved | 34fd5ac | `tests/test_candidate_recovery.py`:无候选拒绝/有候选重验证 resolved;**verify→finish 边界 os._exit 真进程死亡**恢复到 resolved |
| F3(P0) | 测试身份漏类链假通过 | 28f97f0 | `tests/test_test_identity.py` 22 例+真实 pytest 仓库:TestA 请求/TestB 通过不再等价 |
| F4(P1) | 任务 ID 字段裸拼接折叠 | 6c47e92+110369d | `tests/test_task_spec.py`:两种测试分组不同哈希;`tests/test_custom_task.py`:分组/回放不同不 coalesce;issue>500 忠实重建 |
| F5(P1) | 门禁拒后轮次被跳 | 25b8de7 | `tests/test_round_transitions.py`:max_rounds=2 首拒后第二轮必发生;两轮全拒恰好两次 |
| F6(P1) | 空补丁 strict 成功 | b33261f | `tests/test_metrics.py`:空触碰 v2=False/v1=True 双向;strict 语义矩阵 |
| F7(P1) | 演示传旧字段实际失败 | efcf016 | `tests/test_demo_smoke.py`:subprocess 真跑 resolved;diff_text 零出现 |
| F8(P1) | 消融两臂跨引擎混杂 | c9a07a8 | `tests/test_ablation_invariants.py`:共有阶段消息逐字一致/one_shot 无执行反馈/验证失败只有 agent 重试/双跑两臂都跑 |

## 3. 回放基线与收口(spec §6)

| 批 | 命令 | 结果 |
|---|---|---|
| 基线 plain | `python -m app.evals.driver --bugs all --model fake --engine plain --out runs/spec-baseline-plain-20261008` | 35/35 resolved(commit 442cd8d) |
| 基线 graph | 逐题 `run_single --engine graph --out runs/spec-baseline-graph-20261008` | 35/35 resolved |
| 收口 plain | 同命令 → `runs/spec-final-plain-20261008` | 35/35 resolved,bug 去重 35,rounds 全 1 |
| 收口 graph | 同命令 → `runs/spec-final-graph-20261008` | 35/35 resolved(rc 逐题 0),bug 去重 35 |
| 对比 | `scripts/compare_batches.py <基线> <收口> --tools` | **两引擎逐题判定字段 35/35 一致**,退出码 0;轨迹差异为预期机制新增(plain: verify_double_run 0→1;graph: candidate_frozen/final_acceptance 0→1),turns/tokens 合计逐字一致 |

## 4. 攻击样例与门禁

- 10 个攻击样例(`bugs/attacks/`)拦截由既有测试面承担(本轮门禁判定语义零改动,
  仅 S01 收紧测试身份预检——文件/仅类 selector 明确拒绝,合法参数化 id 放行);
- 每卡全量离线 pytest 是提交闸,锚点断言(`tests/test_docs_anchors.py`)强制
  文档与代码同步。

## 5. 演示(spec §6 S08)

| 入口 | 验证级别 | 证据 |
|---|---|---|
| `python -m demo.run_dirty_ticket --out …` | offline verified(subprocess) | tests/test_demo_smoke.py:resolved、源目录哈希不变、patch_text/diff_text 断言 |
| `python -m demo.run_api_ticket --out …` | offline verified(TestClient) | tests/test_api_golden_path.py:全链路+取消+门禁拒绝 |
| 本地 uvicorn+curl | 人工实跑 1 次(2026-10-09 补录,仍未进自动化) | docs/evidence/2026-10-09-uvicorn-http-demo.txt:health→POST→实时进度→FINISHED/resolved→三态验收+候选工件;命令即 demo/README.md §3 |

## 6. 恢复边界(如实声明)

- **已验证**:PROPOSE 内部 turn 边界(既有)+ verify→finish 边界的 `os._exit`
  真进程死亡恢复(tests/test_resume_crash.py、test_candidate_recovery.py);
- **不声称**"任意阶段无损恢复";无候选的旧式 finish 检查点如实 NEEDS_REVIEW;
- 双跑复核只证明配置下的复核一致性,不防恶意仓库伪造 JUnit(threat-model §4);
- local 子进程隔离不是 OS 级安全沙箱;Docker 隔离边界以 docker-isolation-notes 实测为准。

## 7. 实际未完成项(如实列出)

1. ~~**L01 真实效果实验未执行**(未授权)~~ → **2026-10-09 已执行(用户「全部授权」)**:
   Q0 噪声地板批 21/21(7 题 × 3 次,deepseek-flash/docker/串行),判据 **k=1 ≤1 ⇒ 停止付费**,
   Q1/Q2/Q3 按预登记不启动;负结论与逐题数据见 docs/paid-batch-preregistration-2026-10-08.md
   "实测结果 B" 与 docs/evidence/2026-10-09-q0-noise-floor-gen2.txt。
   **仍然成立的约束:无净增益类数字可写**——k=1 恰恰是判据给出的"本证据集不支持净增益主张";
   简历可用的新事实只有测量纪律本身(冒烟→判据门→负结果停手)与噪声地板数字;
2. Python 3.11 兼容未实测(仅 3.14.2);
3. ~~本地 uvicorn HTTP 人工演示未实跑(命令已文档化)~~ → 2026-10-09 已按 demo/README.md §3
   实跑补录一次(docs/evidence/2026-10-09-uvicorn-http-demo.txt),真实 HTTP 栈 golden path
   与 TestClient 结论一致;划线保留是因为它仍未进 pytest 自动化;
4. 722→868 的收集/通过差值全部来自本轮新增回归,存量用例零删除
   (2 个旧钉子按 spec 反转并注明:graph+one_shot 从拒绝变支持、junit 夹具写实化);
   收口后增量:2026-10-09 语料完整性钉 tests/test_bug_corpus.py +2(869→871),
   并修复 BUG-014 reference.diff 存量缺陷(全语料 47 题唯一失同步项);
5. provider 账单对账、多实例部署、Redis 强一致:明确不做(design.md §8);
6. **CI test job 在 ubuntu/py3.11 上持续红(自 ≥M16 起,run #15/#16 实证)**:6 个失败
   分四簇(docker 镜像脱节 ×2 / output_filter 环境敏感断言 ×2 / search_tools 的 rg
   预装分支 ×2),与本轮改动无关(442cd8d 同样失败);第 7 个(S06 测试自身运算符
   优先级 bug,linux 首次执行暴露)已修;本地与 CI 的环境差异及修复卡见 TODO M20。
   **在 CI 转绿前,对本文件的"本地 871 绿"陈述须带上此前提一起读。**

## 8. claim 边界(简历/面试用语约束)

- 平台 resolved **不是** SWE-bench 官方 harness 分数;2026-10-09 L01/Q0 的真实数据是
  **噪声地板与负结论**,不是修复率成绩——"本证据集不支持任何净增益主张"是预登记判据的原文结论;
- "869 passed" 指平台自身离线回归,不是在线业务成功率;
- 恢复能力表述上限:"PROPOSE 内部与 verify→finish 边界的真进程死亡恢复已实测";
- 双跑复核、"预算内"承诺均以其实现语义表述(见 ADR-0009/0010)。
