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
- **并发语义如实声明(P3-10)**:
  - DB 状态的 RUNNING = 执行线程已开始(置位在 `_execute` 首行,提交至线程池
    但仍在排队的任务如实保持 QUEUED),不是"已受理";
  - `task_timeout_seconds`(默认 900s)只是工具循环入口与 turn 边界的预算检查,
    物化仓库/基线/verify/E3 复核不在预算内——合法 graph 任务的墙钟上界
    ≥ 5 轮 × (3 次双跑 pytest + LLM 调用) ≈ 1620s,可超过 900s;
  - 任务锁 TTL = task_timeout + 60 = 960s,同样可被合法长任务超过;TTL 过期后
    持锁方 release 因 token 不匹配变 no-op(不误删他人锁);
  - 因此**防双执行的第一道防线是幂等行读取**(同键在途任务直接返回原任务),
    不是锁——锁过期后同键 create_task 命中幂等分支,根本走不到加锁;
    锁只是"极小窗口内查不到行"时的兜底。多进程共库部署前必须重审本节。
- 崩溃恢复(M6 两级):僵尸任务留有可用循环快照 → 按检查点重新入队续跑;否则判 NEEDS_REVIEW;
- LangGraph checkpoint(SqliteSaver)按 superstep 留档,并作为崩溃恢复的位置权威
  (读侧 `get_state(thread_id)` 见 app/graph/resume.py,A 级工作记忆见 loop_state.py;
  与 app/graph/checkpoint.py 自述一致——无可用快照时仍由 recover_stale 判死)。
- **评测数据读口径(P3-8)**:`tasks` 表 = 服务生命周期真相(状态机/取消/恢复
  都以它为准),`report.json` = 引擎判定取证(逐任务结论/token/门禁明细);
  平台口径以 tasks 为准。evaluations 表已裁删(P1-5 test_runs 同先例:唯一
  写方是任务收尾,全仓零生产读方;取消×自然完成时曾与 tasks/report.json
  三处分裂,审计 R2-Q1)。

## 8. 已知边界(如实记录)

- verify 双跑一致性复核(E3,`verify_double_run` 默认开):成功路径 pytest 拉起
  次数 4→6(基线 2 + verify 第一遍 2 + 复核 2),失败路径不重跑、开销不变;
  价值边界如实声明(P3-5/R3-Q7):防**非自适应偶发伪绿**(收集集漂移/flaky/
  偶发伪造),对基线预置伪造的自适应对手无实质检出力——定位是结构性冒烟复核,
  不是防线;graph 批次佐证已产出(2026-09-26:35/35 题触发双跑三件套,
  见 docs/e3-and-provenance-evidence-2026-09-26.md),真实模型批次佐证亦已补齐
  (2026-10-09 L01/Q0:8 个 resolved run 全部触发双跑复核,mismatch 全 null,
  runs/q0-noise-2026-10-08/);
- **容量模型(E4,如实陈述)**:单进程架构;任务并发上限 = `task_max_workers`
  (默认 2,配置化);local 后端无 CPU/内存配额,失控测试仅受超时杀树约束;
  SQLite 单连接,任务行写入串行。**不据此宣称"支持高并发"**——实测基线:
  10 个 CUSTOM 任务(默认并发 2、pool 内 8 个排队)全部 FINISHED 约 36s
  (tests/test_service_robustness.py 并发冒烟);流量 ×10 时先挂执行资源(审计 3-Q12);
- 多语言适配只有 pytest 一个实现,接口预留;
- token 统计在回放模式下是字符估算;
- 越权拦截率按引擎分口径(P3-13):plain 批门禁拒绝落 PATCH_REJECTED 终态;
  graph 批门禁拒绝经轮内重试、轮尽回滚后落 BUDGET_EXCEEDED——PATCH_REJECTED
  对 graph 永不为终态,按 status 计数会把 graph 批的门禁拦截系统性记 0。
  跨引擎通用口径看 report.json 的 gate_violations/security_blocked 字段
  (评测报告「门禁拦截」计数即源于此);
  攻击样例(10 个,全部拦截)是独立构造集,不混入 bug 集指标;
- **单进程架构**:SQLite 单连接 + 进程内任务锁 + 进程内 CancelRegistry——
  多 uvicorn worker 会破坏幂等/取消语义(取消事件跨进程不可达);
  横向扩展需任务队列与跨进程取消通道,属"平台化"范畴,暂不做;
- **协作式取消粒度**:取消只在工具循环的 turn 边界生效,正在执行的一次
  pytest/LLM 调用(各至多 test_timeout/llm_timeout 秒)会先完成再退出;
  `task_timeout` 是预算检查点而非强杀;
- **`run_pytest(report_path=None)` 的默认值只给测试用,生产调用方必须传报告目录(M11.7,已裁决"不改签名")**:
  传 None 时 junit 与临时根会落进**工作区**(`cwd/.patchpilot_junit.xml[.basetemp-xxxx]`),
  而 `app/gitops/differ.py:30` 用 `git add -A -N` 取 diff(未跟踪文件进 diff)。
  今天它不兑现,靠的是两道**恰好成立**的遮挡:①`.patchpilot_junit.xml` 在物化仓库强制注入的
  忽略清单里(`app/gitops/testing.py` 的 `REQUIRED_GITIGNORE_LINES`);②临时根由
  `finally` 里的 `_discard_basetemp` 回收(git 只报文件不报空目录)。**剩下的真实缺口只有一条**:
  平台进程在 pytest 期间死掉 ⇒ 回收没跑、目录里有文件 ⇒ 续跑后的"修改范围"视图可能带上它们。
  把它改成必填属于签名变更,裁决结果是不改、只在此写明(账目见 TODO 的 M11.7.1);
- **recursion_limit 与 max_rounds 的组合**:graph 引擎的 recursion_limit
  随 max_rounds 推导(M5 引入 PLAN 阶段后为 5N+8——每轮多出计划这一步;此前是 4N+8),
  正常轮数内不会触限;此前硬编码 80 在 max_rounds=20 时会提前误抛。
- **上一条"token 统计在回放模式下是字符估算"只讲了半句,真实批次的完整口径是混单位的
  (2026-10-08 M15 实测,不再是推断)**:累计已耗用走 provider 返回的真值
  (`app/llm/openai_client.py:77-80`,仅当 usage 缺失时才退回估算),而"下一次请求会不会超预算"
  用的是 `app/llm/base.py:47` 的 `len(text)//4` —— 于是 `plain_loop.py:313` 那条门禁
  把**真值累计**和**估算的单次请求**加在一起比。拿 25 份真实模型 run 标定:该估算在真实语料上
  **低估约 1.47 倍**(逐实例 1.263~1.897;`scripts/context_replay.py`,锚点见
  `scripts/measure_context_counterfactual.py` 的 V1 —— 与代码自己算出的估算值对撞,差 0.8~0.9%)。
  后果有界但不为零:门禁因此**晚**判死,误差量级 = 单个请求估算值的 47%(不随轮数累积)。
  2026-10-08 M17 把"要不要统一"变成了一件**可以对照的事**:`Settings.token_estimate_factor`
  只换算那条门禁里"待发的那一次",默认 1.0 = 与本字段引入前逐字同行为(历史读数与 V1 锚点
  不许回溯改口径),取实测 ρ≈1.47 才是 provider 真值口径。**取值仍是待裁决项** —— ρ 是
  随模型与仓库变的一个数,拿它当默认值需要先知道同参重复的运行之间差多大
  (账目见 TODO 的 M17 卡与 `docs/paid-batch-preregistration-2026-10-08.md`);
- **`context_window_tokens` 与 `context_keep_recent_turns` 是联动的,不能单独调**:被钉住的最近 N 回合
  本身就构成一个下限——真实语料上把阈值降到 8000 时,keep=6 有 86%、keep=4 有 52% 的压缩回合
  "压不到位"(调用过压缩但结果仍超阈值;分母取"调用过压缩的回合",含"压到底也没变小"的那些)。
  当前生产默认那组合(16000/6)压得到位率 96%,代价是**额度只省 6%(触发实例中位)**;
  它的锚点代价是 42 个"金补丁文件全文可见"项里 **1 项连路径都不剩**(降到 8000 变 3 丢 + 1 降级)。
  压缩后的请求形态在真实轨迹上 0 条孤儿 tool 消息(扫过 12 个压缩单元格 = 4 阈值 × 3 keep)。
  ⇒ 这两条合起来读:**"阈值 16000"的实际含义是"几乎不动的杠杆"**,不是"省一半";
  账目见 TODO 的 M15 卡与 `docs/evidence/2026-10-08-context-*.txt`。
  崩溃续跑只顺延**剩余额度**(原上限减去检查点已烧步数),已烧完则拒绝续跑并按旧路径判死
  (见 app/graph/resume.py),恢复不换来更多循环余地;
- **停机窗口的受理语义**:服务关停瞬间已受理(create 返回 201)但未起跑的任务,
  可能在线程池关闭后收敛为 NEEDS_REVIEW——冒烟验证(compose_smoke)中实测到该
  竞态,语义为"需要人看",不谎报失败;
- **sock 模式的路径命名空间**:compose 下 docker 执行后端要求 runs 目录
  "容器内路径 = 宿主守护进程视角路径"的 bind 挂载,见 docker-backend-notes.md。
- **轨迹摘要里"无声丢失"这一族:两处都已修掉(2026-10-08)**
  (把整条摘要链扫一遍的结论):`_summarize_input`(`app/tools/registry.py:248`)截长字符串时
  留 `... (N chars)` 标记,`_record_thought`(`app/graph/plain_loop.py:93`)同理;
  而 `_summarize_output`(`registry.py:262`)原先只保留**前 8 个键且不留标记** ——
  扫全 `runs/*/*/trajectory.jsonl`(fake 与真实批次都在内)得到 **941 条 `run_tests` 轨迹事件,
  输出键数全部正好是 8**(源是 `app/tools/execution.py:50` 那八个字段),
  也就是"天花板正在起作用但没人注意到"。
  现在超 8 键会写 `_omitted_keys: "+N keys omitted"`,**8 键以内一字不动 ⇒ 现有轨迹形状零变化**;
  摘要链本身也补了直接用例(`tests/test_trajectory_summary.py`,此前该函数零覆盖)。
  仍然剩下的边界:`_summarize_input` 的 300 字符上限只保证**长度**可复原,不保证全文可见 ——
  要全文得单独落附件,那是另一个决定(M16.9 的取舍即止于此);
