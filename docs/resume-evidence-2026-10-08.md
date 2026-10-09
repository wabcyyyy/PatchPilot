# 简历证据(2026-10-08 spec 收口)

三条可直接用于简历的 bullet,每条都绑定本仓库可核对的实现与测试证据;
**实现先于声明**——任何一条的证据文件被删,对应表述即应撤回。
真实效果实验(L01)未执行,故全部 bullet **不含**修复率/提升类数字。

---

**Bullet 1(架构与上下文)**

> 构建定位—规划—补丁—验证的仓库级 Agent 流水线(LangGraph 状态机),
> 结合 AST 符号检索、仓库骨架先验、滑动窗口上下文压缩与阶段预算/任务级资源账本,
> 管理长程代码调查与修复重试;9 类受控工具白名单 + 块锚定补丁协议约束 Agent 行为面。

证据锚:
- 状态机与节点:`app/graph/nodes.py`、`app/graph/builder.py`(企划书 4.2 转移表);
- 检索/骨架/压缩:`app/tools/search.py`、`app/context/repo_map.py`、`app/context/token_window.py`(ADR-0004/0005);
- 资源账本:`app/graph/resources.py`(S02a:call_id 入账、超额结构化收尾、unknown 不写 0);
- 工具与门禁:`app/tools/registry.py`、`app/graph/gates.py`(七项门禁,10 攻击样例);
- 回归:`tests/test_f1_budget.py`、`tests/test_resources.py`(末回复超预算不再 resolved,F1)。

**Bullet 2(验收链与崩溃恢复)**

> 设计完整任务契约(canonical JSON + SHA256 任务身份)与候选补丁哈希绑定的验收链:
> 完整测试身份匹配、七项质量门禁、双跑一致性复核与资源判定的终局共享验收;
> 实现崩溃后按冻结候选重应用并完整重验的恢复路径,杜绝检查点沿用陈旧成功结论。

证据锚:
- 任务契约:`app/task_spec.py`(S05a/b:F4 两种测试分组不再折叠成同一任务);
- 测试身份:`app/adapters/test_identity.py`(F3:同名类不互认、参数化精确匹配);
- 终局共享验收:`app/graph/acceptance.py`(graph 与 plain 同一条判定代码);
- 候选恢复:`app/graph/candidate.py` + `app/graph/resume.py`(F2:verify→finish 边界
  `os._exit` 真进程死亡后按候选重应用、重门禁、重测试到 resolved);
- 回归:`tests/test_candidate_recovery.py`、`tests/test_time_budget.py`(恢复不重授时间预算)、
  `tests/test_round_transitions.py`(拒绝后第二轮必发生,F5)。

**Bullet 3(服务化与证据基建)**

> 基于 FastAPI/SQLite 实现异步任务、在途幂等(任务契约哈希作幂等键)、受理输入冻结、
> 运行中进度事件实时入账与审计产物;建设 35 题离线回放回归、10 个攻击样例与
> 同引擎消融入口(策略对象 + 未登记差异即报错的比较器),以版本化指标与
> 逐题可核的测试报告支撑结果。

证据锚:
- 服务:`app/api/service.py`(幂等/取消原子守卫/僵尸恢复分流)、`app/api/preflight.py`
  + `app/gitops/input_snapshot.py`(S06:受理冻结副本,执行只读副本);
- 实时进度:`app/tools/tracker.py` sink → SQLite(S07:运行中可见 stage,终态不可被
  迟到事件复活);
- 指标:`app/evals/metrics.py` v2(F6:空补丁不再 strict 成功,历史口径可显式重算);
- 消融:`app/evals/experiment_policy.py` + `scripts/compare_experiments.py`
  (F8:同引擎两臂,未登记差异即报错);
- 回放证据:`runs/spec-{baseline,final}-{plain,graph}-20261008`(35/35 逐题一致)、
  `docs/release-evidence-2026-10-08.md`(全部命令/退出码/数字索引)。

---

## 使用边界(不可超越证据的表述)

- 不写"显著提升修复率/SWE-bench 跑分/生产级高并发/任意阶段无损恢复/严格保证不超预算";
- 平台 resolved 是自定义判定规则下的结论,与 SWE-bench 官方 harness 无关;
- 测试数字表述为"平台离线回归 869 passed"(最终 HEAD 实测),不是在线成功率;
- 真实模型在线实测为零;历史 runs/swe-* 的 5/5 属挑选小样本+平台自判定,
  只能以"接入过真实模型闭环验证"表述,不得当作一般修复率。
