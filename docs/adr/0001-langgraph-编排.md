# ADR 0001:编排层选型 —— LangGraph StateGraph + 自研 turn 循环

日期:2026-09-25 | 状态:已采纳 | 素材:interview-audit-2026-09-24.md 第 2 轮 2-Q4

## 背景

任务状态机(企划书 4.1/4.2)有显式转移表:prepare→baseline→localize→propose→
apply→verify→(finish|rollback),条件路由包括 PATCH_REJECTED 按轮数重试、
预算耗尽终止、NEEDS_REVIEW 终点;节点级断点恢复与 recursion_limit 联动是硬需求。
审计 2-Q4 承认:"与 AutoGen/LangChain 的对比没有留下论证记录"——本篇补齐。

## 候选

1. **AutoGen**:对话式多代理编排,控制流隐含在代理间消息往来里;
2. **LangChain(Chain/LCEL)**:线性流水线抽象,条件循环与状态回滚表达力弱;
3. **LangGraph StateGraph**(采纳);
4. **纯手写**:dict 状态机 + 自研遍历。

## 结论

StateGraph 承载显式转移表与条件边(builder.py 与企划书 4.2 一一对应),
节点内工具循环(run_plain_loop)自研——因为资源门禁(token/时间/轮数)必须在
turn 边界复查、可用工具白名单按阶段不同(READ_TOOLS/WRITE_TOOLS),
这两点通用框架没有现成挂点。checkpointer(SqliteSaver)仅作轨迹留档/调试,
不提供崩溃恢复(P3-7 如实化:恢复=recover_stale 把僵尸任务收敛 NEEDS_REVIEW,
从不按 thread_id 重放,与 app/graph/checkpoint.py 自述一致);
recursion_limit 随 max_rounds 推导(4N+8)防长任务误抛。

## 后果(含反方)

- +:转移表可审计、与门禁/回滚语义严格同构;checkpoint 仅留档,
  崩溃恢复由 recover_stale 收敛 NEEDS_REVIEW 承担(如实表述,见验证锚点);
- −(反方):LangGraph API 迭代快、依赖较重,单机工具用不到其分布式能力;
  SqliteSaver 连接需手工管理(R2 整改踩过 Windows 句柄泄漏),这是自研
  循环本来没有的成本;
- 若未来证明状态机只有 3~4 个节点,迁移到纯手写的成本可控(节点函数是纯闭包)。

## 验证锚点(P3-7 防复发)

以下 file:line 锚点由 `tests/test_docs_anchors.py` 逐条断言;文档或代码改动使
锚点失配时测试失败——用「全量 pytest 绿」红线物理卡住"改代码不改文档"。

- `app/graph/checkpoint.py:3` — `当前没有崩溃恢复路径`
- `docs/design.md:51` — `仅作轨迹留档`
- `docs/design.md:67` — `9 个`
- `docs/threat-model.md:45` — `攻击样例 9 个拦截`
- `README.md:20` — `9 个攻击样例`
- `app/graph/gates.py:1` — `七项门禁`
- `docs/adr/0002-execution-backend-local-默认.md:31` — `无留档的实测运行`

