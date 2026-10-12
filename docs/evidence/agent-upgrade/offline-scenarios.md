# 离线场景集索引(G16)

SPEC G16 要求离线集覆盖十三类场景。它们早已分散在测试套里,本文件把它们**点名对齐**:
每一行是一个场景与它的离线(FakeLLM/回放,零网络零付费)验收入口。配套的
`tests/test_offline_scenarios.py` 会核对表中每个测试入口真实存在——清单烂了测试会红,
不许"文档里有、套件里没有"。

| # | 场景 | 离线验收入口 | 钉住什么 |
|---|---|---|---|
| 1 | 单文件修复 | `tests/test_graph.py::test_graph_resolves_bug001` | 图引擎 resolved:补丁应用 + 双集通过 |
| 2 | 跨文件修复 | `tests/test_driver.py::test_replay_resolves_cross_file_bug005` | plain 引擎跨文件补丁判定 |
| 3 | 长输出 | `tests/test_f1_budget.py::test_graph_last_reply_over_budget_never_resolves` | 超长回复不构成 resolved,按预算语义收敛 |
| 4 | 错误假设 | `tests/test_graph.py::test_graph_wrong_failed_test_id_never_resolves` | 带错测试 id 的题目永远不得 resolved |
| 5 | 无补丁/放弃 | `tests/test_graph.py::test_graph_verify_failed_when_agent_gives_up` | 放弃声明 → VERIFY_FAILED,不冒充成功 |
| 6 | 身份错配 | `tests/test_task_spec.py::test_run_task_graph_rejects_source_changed` | 源指纹漂移 → INVALID_TASK,不静默执行新内容 |
| 7 | 门禁拒绝 | `tests/test_graph.py::test_graph_last_round_gate_rejection_preserves_evidence` | 末轮被拒仍保全证据;演示见 `demo/run_issue_only_ticket.py` |
| 8 | 环境失败 | `tests/test_driver.py::test_crash_converges_to_needs_review` + `tests/test_preflight.py::test_preflight_classifies_missing_dependency_as_collection_error` | 崩溃/环境失效如实 needs_review/受理拒绝 |
| 9 | 预算停止 | `tests/test_graph.py::test_graph_budget_exhausted_after_failed_verify` | 预算耗尽是终点,末轮现场保全(diff.patch 非空) |
| 10 | 输入等待 | `tests/test_waiting_input.py::test_answer_resumes_waiting_task_and_reaches_resolved` | ask_user 等待 → 回答唤醒 → resolved |
| 11 | 取消 | `tests/test_cancellation.py::test_cancel_stops_running_task` + `tests/test_api.py::test_cancel_finished_conflicts` | 运行中取消生效;终态取消冲突;演示见 `demo/run_api_ticket.py` |
| 12 | 恢复 | `tests/test_resume_crash.py::test_resume_survives_an_unclean_process_death` | os._exit 硬死 → 检查点续跑 → 复位 + 门禁/验证重跑;演示见 `demo/run_restart_resume_ticket.py` |
| 13 | 落选分支 | `tests/test_branching.py::test_all_candidates_without_patch_falls_back_to_single_line` | 全部落选回退单线;消耗入账见 `tests/test_subagent_wiring.py::test_all_candidates_losing_charge_subtask_ledger` |

补充(委派端到端):`tests/test_subagent_wiring.py::test_end_to_end_delegate_resolves`
钉三角色委派的完整链路;演示见 `demo/run_delegate_ticket.py`。
用户纠偏:`tests/test_waiting_input.py::test_steer_during_wait_is_a_clue_not_a_contract_change`;
演示见 `demo/run_steer_ticket.py`。issue-only 全流程(诊断→批准→重试→报告):
`tests/test_diagnosis_intake.py::test_approve_endpoint_freezes_manifest_and_task_resolves`;
演示见 `demo/run_issue_only_ticket.py`。

## 本索引不许被读成的话

- 这些是**协议与边界**的离线证明。修复率、轮次/成本收益、窗口配比是否合理,属于
  G17 真实端点批的问题——FakeLLM 的 resolved 数不得被宣读成真实修复率。
- 场景覆盖 = 存在钉住该行为的测试;不等于"所有真实失败模式都被枚举"。
