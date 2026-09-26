# 第二轮对抗审计整改报告(2026-09-26)

> 工作方式:对照 docs/interview-audit-2026-09-26.md 的 18 条真问题(P3-1..P3-18),
> 按「第一批文档止血 → 第二批小代码高价值 → 第三批人工触发」三批落地,每卡一个
> Conventional Commits commit,提交前跑绿 `PATCHPILOT_LLM_ENABLED=false pytest -q`
> 与 `ruff check .`。标注口径:**已修** = 修复落地且带测试(或纯文档已如实化);
> **部分** = 文档/链路已就绪但证据本体待人工触发;**未动** = 属门禁/部署语义变更或
> 结构变更,等人工确认(见 docs/人工触发清单-2026-09-26.md)。
> 不把「给了方案」记成「已落地」。

## 0. 基线与结果

- 起点:d22ea64(审计报告入库),290 passed + 2 skipped(本机无 Redis),ruff 全绿;
- 终点:HEAD 见 §2 提交清单,全量 pytest 绿(数字见文末「验证」节),ruff 全绿;
- 新增测试:test_docs_anchors.py(4)、test_model_name_guard.py(6)、
  test_logging_setup.py(6)、test_recycle.py(5)、test_validate_candidate.py(3)
  及既有文件内新增用例(test_storage 方向 c、test_report 判型/软集警报、
  test_driver 分类快照/graph 拒绝、test_graph 错 id、test_docker 错 id、
  test_service_robustness 排队语义 ×2)。

## 1. 逐条对照(P3-1..P3-18)

| 编号 | 结论 | 落地内容 | 证据 |
|---|---|---|---|
| P3-1 真实模型批次为零 | **部分(链路就绪)** | 真实跑批需 API 花费,入人工触发清单第 1 项;守卫链已保证 report.json 必带 model_name | 人工触发清单 §1 |
| P3-2 溯源锚点为脏工作树 | **已修(fake 批;真实批锚点随 P3-1)** | ①provenance/manifest 增记工作树状态(`worktree_dirty`/`dirty_fingerprint`,None=无法判定、tracked-only 语义);②manifest 落盘前 `git ls-tree` 反查输入存在性(缺失留痕+WARNING);③干净树重跑 fake 35 题基线批(35/35 resolved、`worktree_dirty=false`、`input_anchor_missing=[]`);④反向验证:新检查指向历史坏锚 367ae36 精确报出 BUG-029..035(7/7,与审计 R1-Q2-1 一致) | app/evals/provenance.py、app/evals/driver.py、tests/test_provenance_anchor.py(11 例);docs/e3-and-provenance-evidence-2026-09-26.md §B/§C;commits 8fd9e85/ae3db2c |
| P3-3 守卫复活链 | **已修** | require_model_name 守卫下沉 openai_client 构造(llm_model 非空,先于 openai 包导入)+ run_task_graph 第四入口补同口径守卫;fake 豁免语义保留(夜志 E2 理由);API+graph/API+plain/run_single/直连 graph 四链测试 | app/llm/openai_client.py、app/graph/runner.py、tests/test_model_name_guard.py;commit 9f26649 |
| P3-4 finalize 弱守卫 | **已修** | 守卫改 NOT IN 全终态(谁先到终态谁赢),NEEDS_REVIEW 复活方向测试;多进程部署附注留档(人工触发清单 §6) | app/storage/repository.py finalize_task、tests/test_storage.py;commit 263d969 |
| P3-5 E3 零佐证+口径互斥 | **已修(文档+graph 佐证;真实模型佐证随 P3-1)** | ①文档:config.py「攻击成本翻倍」废弃,nodes/ADR-0002/threat-model §4/design.md §8 统一降格为「结构性冒烟复核,防非自适应偶发伪绿」(commit bef502e);②graph 批次佐证已产出(经用户批准零花费执行):35/35 题触发双跑,`verify_double_run` 轨迹事件 35/35(此前全 runs 树 0 条)、checkpoints 物证 35/35、mismatch 全 null;证明力边界如实声明=证明机制连通不证明检出力 | app/config.py、docs/design.md §8、docs/e3-and-provenance-evidence-2026-09-26.md §A |
| P3-6 软链残余风险 | **部分(文档已修,门禁待确认)** | threat-model §3 R3a 残余段 + §4 边界补记(commit 184f227);「120000 一律拒」属门禁语义变更(AGENTS 红线),方案要点写入人工触发清单第 5 项 | docs/threat-model.md R3a;人工触发清单 §5 |
| P3-7 文档互斥 7 处 | **已修 + 防复发机制** | 崩溃恢复 ×3(ADR-0001:24/29、design.md)如实化;「8 个」→9 ×3;「六项」→「七项」×2;ADR-0002:31 compose 冒烟如实化;test_docs_anchors.py 锚点断言(ADR 末尾「验证锚点」节 + file:line 级断言 + 假命题黑名单) | tests/test_docs_anchors.py;commit c666215(锚点随编辑漂移同步 ×4:3f899f7 等) |
| P3-8 evaluations 三处分裂 | **已修** | 表/迁移/upsert/list/用例全删(P1-5 先例);读口径「tasks=生命周期真相,report.json=引擎取证」写入 design.md §7 | app/storage/db.py、repository.py、service.py;commit 71ac776 |
| P3-9 日志零接线 | **已修(装配)/另立卡(per-event)** | Settings.log_level(带校验)+ app.py _setup_logging(root 无 handler 才接线)+ TaskContextFilter(contextvar)+ _execute 线程首行设上下文 + lifespan 空 token 启动告警(诚实声明:检测不了实际绑定地址);per-event request_id 重构如实记录为另一张卡 | app/logctx.py、app/api/app.py、tests/test_logging_setup.py;commit d0f3a5d |
| P3-10 并发语义 | **已修** | 置 RUNNING 挪入 _execute 首行(排队如实保持 QUEUED,取消早退不复活不产产物);design.md §7 显式声明:900s 只盖 turn 边界、合法 graph 任务 ≥1620s 超锁 TTL 960s、防双执行靠幂等行非锁 | app/api/service.py、docs/design.md §7;commit 319bc26 |
| P3-11 snapshot 白名单 | **已修** | 补 verify_double_run(git 考古证实为遗漏)/task_timeout_seconds/default_max_rounds;Settings 键三分类(快照/密钥/豁免)快照测试——新键不三选一即 CI 失败;max_turns 升格记录为结构变更不实施 | app/evals/provenance.py SNAPSHOT/SECRET/EXEMPT_KEYS、tests/test_driver.py;commit f35628d |
| P3-12 「永久保留」不可持续 | **已修** | tracker.py 与企划书承诺改两档(取证集永久/可弃集回收);app/api/recycle.py 终态回收器在 _execute 终态回写后即时回收(仅 FINISHED 且赢终态竞争——不放 finally 子句,崩溃/取消现场必须留给复盘)+ 启动 Grace 扫描补收停机漏收;Windows 实测两类占用分治:git objects 只读(WinError 5)整树去只读、sharing violation(WinError 32)退避重试 | app/api/recycle.py、tests/test_recycle.py;commit f8d9c65 |
| P3-13 判型口径 | **已修** | design.md 分引擎口径(graph 批 PATCH_REJECTED 恒不为终态);report.py 判型四类互斥(resolved/needs_review 按 verdict,门禁拦截按 gate_violations),graph 批门禁拒绝不再被记 0 | app/evals/report.py、docs/design.md;commit 920964a |
| P3-14 评测治理 | **已修(文档与机制)/清单(外部复核)** | checklist「修法唯一」改操作化标准「无误杀」(review-2026-09-25.md 加整改注记,不改历史表);preflight --log-dir 逐题原始输出落盘 + 3 用例;外部复核/双盲 checklist 入人工触发清单 §6(人力) | scripts/validate_candidate.py、bugs/candidates/README.md;commit 752cb32 |
| P3-15 证据源不可达 | **部分(README 已修,对照批入清单)** | README「以 CI 最新跑批为准」(origin/master 落后 38 commit)改本地可复现命令(commit f55e3ae);真实盲跑对照批入人工触发清单第 2 项 | README.md 目录结构节、人工触发清单 §2 |
| P3-16 假 graph | **已修** | driver --engine choices=["plain"]、report.py 引擎兜底 "graph"→"unknown"、「unknown 命令不可执行」口径保留、空批示例改 plain;三个钉死测试同 PR | app/evals/driver.py、report.py、tests/test_driver.py::test_batch_cli_rejects_graph_engine、tests/test_report.py;commit 0c2591c |
| P3-17 缺测 | **已修** | 容器路径错 id 用例(test_docker,守卫不可用则跳过)+ 判定层集成用例(plain/graph 带错 id → 终态不得 resolved;graph 实测收敛 VERIFY_FAILED,与 R3-Q5 草案的 BUDGET_EXCEEDED 差异如实记录在用例 docstring);parametrize 假阴性方向只记录不修(安全侧) | tests/test_docker.py、tests/test_graph.py、tests/test_driver.py;commit 0ac6520 |
| P3-18 基线断言 | **已修** | E4 冒烟实测 39.7s + 机器元数据落盘 tests/baselines/e4_smoke.json,断言 基线×3.0(180s 硬闸保留为防死锁);「全绿全 1 轮」真实模型批(≥5 题)触发报告级软集形态警报,fake 批豁免,3 场景钉死 | tests/baselines/e4_smoke.json、tests/test_service_robustness.py、app/evals/report.py;commit f52a822 |

**汇总:已修 15 条(P3-2/3/4/5/7/8/9/10/11/12/13/14/16/17/18),部分 3 条
(P3-1 真实模型证据、P3-6 门禁语义变更待确认、P3-15 真实盲跑对照批——
三项均需用户付费或拍板,见人工触发清单),未动 0 条。15 + 3 = 18,逐条对齐。**

> 补记(2026-09-26 第二轮收尾):P3-2 与 P3-5 原列「部分」,其补证实验实际
> 零花费(FakeLLM 回放),经用户批准后执行完毕,升至「已修」;真实模型批次的
> 佐证仍归 P3-1(付费项)统一产出。

## 2. 提交清单(d22ea64..HEAD,每卡一 commit)

| commit | 卡 | 内容 |
|---|---|---|
| c666215 | P3-7 | 文档止血 + 锚点测试 |
| 920964a | P3-13 | 判型口径分引擎 |
| 319bc26 | P3-10 | RUNNING 挪入 _execute + 并发语义声明 |
| f55e3ae | P3-15 | README 指向本地可复现命令 |
| 752cb32 | P3-14 | checklist 无误杀 + preflight 落盘 |
| 9f26649 | P3-3 | 守卫下沉 + 四链测试 |
| 263d969 | P3-4 | 终态守卫 NOT IN 全终态 |
| 71ac776 | P3-8 | 裁删 evaluations 表 |
| d0f3a5d | P3-9 | 日志装配 |
| f35628d | P3-11 | snapshot 白名单 + 三分类测试 |
| 0c2591c | P3-16 | 假 graph 收口 |
| 0ac6520 | P3-17 | 缺测补齐 |
| f52a822 | P3-18 | E4 基线断言 + 软集警报 |
| f8d9c65 | P3-12 | 终态产物回收 |
| 184f227 | P3-6 | threat-model R3a 残余段 |
| bef502e | P3-5(文档) | E3 表述降格 |
| 3f899f7 | P3-7 锚点同步 | ADR-0002 行号漂移 |
| 8d5cb96 | P3-12 表述精度 | 回收挂载点措辞 |
| 8c8f39f | P3-12 flake | 测试原子性假设的根因修复 |
| 64ee7f6 | 收尾 | 验证数字 |
| 8fd9e85 | P3-2 | 脏树指纹 + manifest 锚点反查 |
| ae3db2c | P3-2 校准 | worktree_dirty 语义校准为 tracked-only |
| (本次) | 补证收尾 | 零成本实验证据文档 + 本报告 P3-2/P3-5 改判 + 清单/索引同步 |

## 3. 审计之外的同步修订(文档与代码同 PR 纪律)

- **P3-12 迭代中捕获的 flake(根因与修法记录在案)**:全量 pytest 曾出现 1 次
  `test_service_recycles_workspace_on_finished` 失败(隔离跑恒过)。根因不是回收
  逻辑坏,而是**测试假设了原子性**:终态回写先于回收完成,轮询方可能在极短窗口内
  读到 FINISHED 而可弃集尚在。用探针(注入慢回收)确定性复现了该窗口;
  修法=断言强度不变(仍要求"必须回收"),改有界等待,并把窗口本身做成确定性
  用例(注入 1s 慢回收 + 有界取证)钉住语义;recycle.py docstring 补记
  「最终一致,非瞬时」。**没有**改实现去迁就测试——回收仍挂在终态回写后
  (放进 finally 或提前到 finalize 前会破坏"取消/崩溃现场必须保留"语义);
- **第 8 处文档互斥(复核中新增发现)**:审计 R3-Q3 裁定的「config 注释是唯一与
  事实矛盾处」在逐卡复核中被落实——`app/config.py` 原注释「容器集成留待人工验证」
  与 docker 后端已接线(09-18 端到端产物 runs/docker-e2e/、docker-backend-notes.md
  「已接线并真机验证」)矛盾;`docs/README.md` 索引同句(「接线仍未完成,见 audit P2-1」)
  一并如实化,ADR-0002 锚点随之更新并加 pin。**诚实边界**:docker-e2e 产物早于 E2,
  其 report.json 无 provenance 字段,AI 无法从产物独立复核容器执行——如实化依据是
  docker-backend-notes 与审计裁断,不是本次实测;
- docs/adr/0002 E3 表述降格后 compose 冒烟锚点行号漂移——按锚点测试要求同步;
- design.md 三次编辑(并发语义/读口径/E3 边界)均同步了 test_docs_anchors.py
  的行号锚点——这正是锚点机制的设计行为:文档改动必须显式过锚点关;
- **零成本补证实验(2026-09-26 第二轮收尾,经用户批准)**:P3-2/P3-5 的补证实验
  原被表述为"要真实 API 花费",复核发现其实际用 FakeLLM 回放=零花费,遂执行:
  graph+E3 连通性批 35/35 触发三件套 + 干净树 fake 35 题基线批(锚点反查全过);
  反向验证新检查在历史坏锚 367ae36 上精确报出 BUG-029..035(7/7)。证据与方法见
  docs/e3-and-provenance-evidence-2026-09-26.md;找到并修正的自身问题:首版
  `worktree_dirty` 把未跟踪文件也计入(本仓库长期有 .idea//demo/ → 标志恒真、
  失去操作性),已校准为 tracked-only 并补边界用例(commit ae3db2c)。

## 4. 明确不做(记录在案,不做=决策不是遗漏)

- max_turns 升格 Settings(P3-11 附注,结构变更);
- 「new file mode 120000 一律拒」(P3-6,门禁语义变更,等确认);
- per-event request_id 结构重构(P3-9 附注,另立卡);
- 多进程共库部署支持(P3-4 附注,守卫语义需重审);
- 真实 API 花费类证据产出(P3-1 真模型批 / P3-15 盲跑对照批,人工触发清单 §1/§2;
  P3-2/P3-5 的补证实验原列此处,复核后确认零花费并已执行,见 §3);
- parametrize 展开假阴性(parametrize 用例通过时被逐实例误判为不过,安全侧低危,
  P3-17 附注:只记录不修)——记录于 tests/test_driver.py 对应用例 docstring
  与人工触发清单 §6;
- 《PatchPilot开发计划书》中残留的「六项门禁」字样(P3-7 范围外):该文档是历史
  任务卡原文本,第 222-223 行已显式披露「2026-09-20 审计后代码实际为七项」,
  且 docs/README.md 明确其复选框/正文不作为状态依据;按 AGENTS「只修改任务卡
  声明范围内的文件」不越界改动。

## 5. 验证

- `PATCHPILOT_LLM_ENABLED=false .venv/Scripts/python.exe -m pytest -q`:
  **324 passed + 2 skipped**(起点 290+2;净增 34 个用例 = 新增 36 − 随表删除 2;
  2 个 skip 为本机 6379 无 Redis 的预期跳过);**修完 P3-12 flake 后连续 3 次全量
  通过(415.94s / 422.90s / 全绿,exit 0)**——flaky 的根因、复现与修法见 §3;
  2 skip 为本机无 Redis 的预期跳过;
- `.venv/Scripts/python.exe -m ruff check .` 与 `ruff format --check .`:全绿
  (138 files already formatted;唯一 unformatted 是 `demo/run_dirty_ticket.py`
  ——任务开始前就存在的未跟踪用户文件,不在本任务声明范围内,未改动);
- 锚点/分类/守卫/回收/日志五个防复发机制全部有测试钉住:
  test_docs_anchors.py(4)、test_driver.py 分类快照(1)、test_model_name_guard.py(6)、
  test_recycle.py(5)、test_logging_setup.py(6);溯源硬化另有
  test_provenance_anchor.py(11);
- **零成本补证实验的判据核验(2026-09-26 收尾)**:graph+E3 35/35 触发三件套、
  干净树基线批 35/35 resolved 且 `worktree_dirty=false`/`input_anchor_missing=[]`、
  历史坏锚反向验证 7/7 命中——证据与方法见 docs/e3-and-provenance-evidence-2026-09-26.md。
