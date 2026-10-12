# B6 / G16 发布证据索引(2026-10-12)

SPEC G16 的发布节点证据。收据本体是机器生成的
[release-receipt.json](release-receipt.json)(`scripts/release_receipt.py`,发布节点运行);
本文件是它的导读:每份证据是什么、在哪、说明了什么、**没**说明什么。

## 被测身份

- HEAD:详见 release-receipt.json 的 `run_identity.platform_commit`(C6 代码提交后生成);
- 生成时工作树的**非本批次**脏文件(用户并行改动)原样列在 `run_identity.dirty_files`,
  不声称"生成于干净树"。

## 全量测试

- 日志:[b6-full-pytest-2026-10-12.log](b6-full-pytest-2026-10-12.log)(`-m "not stress"`,
  本地全量;全集含 stress 由 CI 执行);
- 读数:**1276 passed / 0 failed / 0 skipped / 1 deselected,exit 0,21:07**;
  解释器 `.venv` Python 3.14.1 / pytest 9.1.1 / ruff 0.17.0,basetemp `D:/tmp/pt`;
- 诚实记录:第一次全量 4 failed(loop_settings 新键未同步进 verbatim 钉子、
  single_shot 未接新 kwarg、plan_stage 与 model_profiles 两处测试的补丁位置未跟上
  冻结契约语义),单独复现修复后**重跑全量**得本收据;两份日志只有第二份是收据;
- Docker:本机守护进程可用,此前 skip 的容器用例本轮**真实执行**并通过
  (B5 收据是 31 skipped,本轮 0 skipped)。

## 逐场景结果(离线,FakeLLM,零网络零付费)

| 场景 | 演示入口 | 结果 |
|---|---|---|
| dirty-ticket(引擎直连最小闭环) | `demo.run_dirty_ticket` | OK(见收据秒数) |
| API golden path + 取消 + 门禁拒绝 | `demo.run_api_ticket` | OK |
| issue-only 全流程(诊断→批准→反馈重试→报告) | `demo.run_issue_only_ticket` | OK |
| 用户澄清与纠偏 | `demo.run_steer_ticket` | OK |
| 子任务委派 | `demo.run_delegate_ticket` | OK |
| 崩溃恢复 | `demo.run_restart_resume_ticket` | OK |

演示文字步骤与分工表:[demo/README.md](../../../demo/README.md)。
离线测试场景与测试入口的对齐表:[offline-scenarios.md](offline-scenarios.md)
(防腐测试 `tests/test_offline_scenarios.py` 保证表格不烂)。

## 能力与契约

- 能力查询面:`GET /api/capabilities`(读数与受理层同源,one_shot 如实登记为
  评测 CLI 专用)——同源断言在 `tests/test_capabilities.py`;
- 冻结契约:执行与恢复从受理时冻结的 TaskSpec 重建有效策略,同名 profile 数值漂移
  不改变已受理任务口径(`tests/test_frozen_policy.py`,含环境漂移整链用例);
- 迁移与兼容:旧 SQLite 行判待审、旧 loop 快照按既有语义续跑、custom 任务恢复从
  契约复原回放脚本(`tests/test_legacy_migration.py`,os._exit 硬死子进程)。

## 已知限制(不许被读成"已证明")

1. **release_incomplete_for_container_execution = false(本轮)**:容器用例已在本机
   真实执行;但"声明环境"若指 CI 的 Linux 容器矩阵,以 GitHub Actions 的运行记录为准,
   本文件不代 CI 宣言;
2. 离线收据只证协议与边界:修复率、轮次/成本收益、窗口配比最优性属 G17 真实端点批,
   FakeLLM 的 resolved 数不得宣读为真实修复率;
3. repair_v2 的 64k 目标窗口/keep=12、max_identical_failures=3、委派并发=2/每任务 3 次
   是工程起点,无真实负载读数;
4. 在线 cost/hybrid 预算闸的离线验收不覆盖真实计费链路;
5. 能力/角色/预算的"真实有效值"以 `GET /api/capabilities` 与登记表为准,文档转述
   (包括本文件)不是权威源。

## 发布状态

B6(G16)交付后,窗口、交互、subagent、恢复、能力查询是**实际接线能力**而非文档名词:
每一项都有离线端到端演示(上表)与防腐测试。下一批 G17(在线批)需单独授权,
本收据不构成任何真实模型调用的授权或结果。
