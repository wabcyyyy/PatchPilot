# PatchPilot 设计笔记

记录关键设计决策与取舍,供面试阐述与后续演进参考。规范定义以《PatchPilot项目企划书》为准。

## 1. 为什么状态机先行,且拆成"定位/补丁"两个阶段?

单循环 Agent(plain_loop)只有一个隐式状态"对话历史",无法表达
"基线已确认失败"、"这一轮补丁被门禁拒绝过"这类平台事实。
LangGraph 状态机(企划书 4.2 转移表)把循环限制在 PROPOSE ↔ APPLY ↔ VERIFY,
轮数/时间/token 预算作为守卫;LOCALIZE 阶段禁用写工具,
从机制上把"先理解再动手"变成约束而非建议。

## 2. 判定规则是唯一事实源

`resolved` = 原失败测试全转通过 **∧** 回归集保持通过 **∧** 门禁全通过 **∧** 未超预算。
四个条件分布在 apply/verify/budget 三处检查,但判定语义只有一份(企划书 4.3);
模型的 `finish(success=true)` 只是声明,平台验证不采信声明。

## 3. 门禁分两层

- 工具层(`apply_patch` 工具):对模型提交的每个补丁做静态门禁 + `git apply --check`;
- 节点层(APPLY 节点):对**整个工作区 diff** 复核一遍,防"多次合法小补丁拼出越权大改"。
  两层共用 `app/graph/gates.py` 的同一实现。

## 4. 回放模型(FakeLLM)的定位

`provider=fake-replay` 的脚本回放只用于:离线确定性测试、平台闭环验证。
它让"评测流水线本身"可以被测试(自举),评测报告里始终标注 provider;
真实成绩必须来自真实模型运行——这是"不使用没有来源的漂亮数字"的工程化表达。

## 5. Bug 题目集是纯工作树

`bugs/*/repo` 不含 `.git`:主仓库可以完整跟踪题目内容;
git 历史(基线 commit)在任务运行时由 `materialize_repo` 现场物化,
commit 内容由文件字节决定,天然可复现。基线校验(failed 必须失败、
regression 必须绿)由生成器与测试双重把关——回归集在基线不绿,
判定规则就没有地基。

## 6. 工作区纯净度三原则

1. 任务在源仓库的**副本**上进行,源仓库只读;
2. 运行副产物(junit、basetemp)一律落在工作区外的报告目录;
3. 物化仓库强制注入 `.gitignore`,副产物即使产生也不进 diff 视图。
   模型看到的世界和门禁看到的世界必须干净且一致。

## 7. 服务化的幂等与恢复

- 幂等 = "同键(bug+engine+model)任务在途时直接返回" + 任务锁(Redis NX / 内存兜底),
  终态任务允许重跑——因此 `idem_key` 不设 UNIQUE 约束;
- 崩溃恢复:服务启动把 RUNNING/QUEUED 僵尸任务标记 NEEDS_REVIEW;
- LangGraph checkpoint(SqliteSaver)提供节点级恢复,二者互补。

## 8. 已知边界(如实记录)

- verify 双跑一致性复核(E3,`verify_double_run` 默认开):成功路径 pytest 拉起
  次数 4→6(基线 2 + verify 第一遍 2 + 复核 2),失败路径不重跑、开销不变;
- 多语言适配只有 pytest 一个实现,接口预留;
- token 统计在回放模式下是字符估算;
- 越权拦截率在评测批次里体现为 PATCH_REJECTED 计数,
  攻击样例(8 个,全部拦截)是独立构造集,不混入 bug 集指标;
- **单进程架构**:SQLite 单连接 + 进程内任务锁 + 进程内 CancelRegistry——
  多 uvicorn worker 会破坏幂等/取消语义(取消事件跨进程不可达);
  横向扩展需任务队列与跨进程取消通道,属"平台化"范畴,暂不做;
- **协作式取消粒度**:取消只在工具循环的 turn 边界生效,正在执行的一次
  pytest/LLM 调用(各至多 test_timeout/llm_timeout 秒)会先完成再退出;
  `task_timeout` 是预算检查点而非强杀;
- **recursion_limit 与 max_rounds 的组合**:graph 引擎的 recursion_limit
  随 max_rounds 推导(4N+8,固定前缀 3 步 + 每轮 4 步 + 收尾 1 步),
  正常轮数内不会触限;此前硬编码 80 在 max_rounds=20 时会提前误抛;
- **停机窗口的受理语义**:服务关停瞬间已受理(create 返回 201)但未起跑的任务,
  可能在线程池关闭后收敛为 NEEDS_REVIEW——冒烟验证(compose_smoke)中实测到该
  竞态,语义为"需要人看",不谎报失败;
- **sock 模式的路径命名空间**:compose 下 docker 执行后端要求 runs 目录
  "容器内路径 = 宿主守护进程视角路径"的 bind 挂载,见 docker-backend-notes.md。
