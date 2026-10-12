# PatchPilot 第二轮对抗性双子 Agent 审计报告(2026-09-26)

> 与上轮(2026-09-24「零读码面试」)不同,本轮为**双子 Agent 对抗审计**:面试官出题前先亲自读码找靶,答辩人逐题带证据作答并全程只读,面试官判题时再抽查核实答辩引用。主会话仅做编排与记录,不下结论。
> 三轮共 23 题。面试官对答辩共抽查 53 处决定性主张(R1 19 + R2 16 + R3 18),**答辩人 0 说谎**;答辩人反向纠正面试官 6 处,**6/6 全部成立**,另证伪面试官 1 条攻击链前提。

## 0. 方法:双子 Agent 架构与判题标准

| 角色 | 设定 | 纪律 |
|---|---|---|
| A 面试官(拷问者) | 苛刻的资深后端面试官,不信任任何口头描述,只认代码与运行证据;每轮出题前先 Read/Grep 找具体靶子(文件/函数/数字),判题时对答辩引用再抽查核实 | 全程只读,不跑全量 pytest |
| B 答辩人(诚实方) | 代表项目当前真实状态答辩的工程师,铁律是诚实高于一切 | 全程只读,不改任何文件;回答前必须亲自核实;必须明说「未实现/没测过/无出处」;每题末附「本轮无法给出证据而明确承认的点」 |

- **判题口径**:✅ 站得住(与代码/运行证据一致)/ ⚠️ 真问题(标高/中/低严重度+证据)/ ❓ 存疑(追问,仍含糊记 ⚠️)。「答辩立住」与「缺口不存在」是两回事——B 诚实承认的缺口照常记 ⚠️。
- **轮次**:R1 出 8 题(验证夜间 GOAL 8 卡整改 + 覆盖 8 维度中的 6 个)→ R2 出 7 题(补齐状态机、配置可观测性两个零覆盖维度 + 深挖 R1 缺口)→ R3 出 8 题(补齐剩余维度配额、收口修法争议、攻击死角首审)。每轮合计每维度 ≥2 问。
- **结构**:每次派生均为全新上下文,角色设定、全部历史问答与判定由主会话全文带入。

## 1. 三轮总览

| 轮 | 题数 | 判定形态 | 说明 |
|---|---|---|---|
| R1 | 8 | **全部 ⚠️**(2 高、5 中、1 低) | 面试官针对「夜间 GOAL 8 卡声称已整改」逐卡开题,查出 4 处宣称-实现裂缝(溯源锚点、守卫口径、软链、双跑文案)+ 4 处口径问题 |
| R2 | 7 | **全部 ✅**(缺口照记) | B 答辩全部经抽查立住;题目指向的 7 个缺口(三存储分裂、RUNNING 语义、日志零接线、snapshot 白名单、P0-2 复活链、假 graph 标记、E3 零佐证)全部真实 |
| R3 | 8 | **全部 ✅**(面试官两处前提被证伪) | B 做了真实实验(Linux 容器软链逃逸实证、Windows git apply 实测、146 份报告全量扫描),纠正面试官 3 处并全部成立 |

三轮对抗的净结果:**问题清单 18 条(P3-1..P3-18,高 5 / 中 10 / 低 3),B 的口头主张 0 条被驳倒,面试官自身的靶子 6 处被纠正**。审计对 B 结论的复核严格于对 B 的复核,这正是对抗设计要的效果。

## 2. 问答与判定精选(按 8 维度)

### 2.1 状态机与任务生命周期(R2-Q1、R2-Q2、R3-Q2)

**R2-Q1 取消 × 自然完成的三存储分裂(缺口:中)**。取消请求在 verify 阶段到达(turn 边界不触发)、任务随后自然完成时:`service.py:263` 的 finalize 返回值被丢弃,`repository.py:125` 守卫仅 `!= 'CANCELLED'`,`upsert_evaluation` 无终态守卫,`driver.py:267` finally 无条件写 report.json——终态为 **tasks=CANCELLED / evaluations=final_resolved=1 / report.json=resolved** 三处分裂;读 report.json 的两条链(`GET /tasks/{id}/report`、`app.evals.report`)会把它读成 resolved。evaluations 表 grep 全仓**零生产读方**。R3-Q2 给出裁断:evaluations 表按 P1-5 先例(test_runs 表零读方被删)应删除;读口径「tasks=服务生命周期真相,report.json=引擎判定取证,平台口径以 tasks 为准」写入 design.md §7;finalize 守卫收紧为全终态不可覆写,与 cancel 语义不冲突。

**R2-Q2 RUNNING 语义与 900s 预算的真实覆盖段(缺口:中)**。置 RUNNING 在 `service.py:168` 发生在 submit(:171)之前——「RUNNING」的操作化语义是「已受理进线程池」,池内排队在 DB 不可见(E4 冒烟 10 任务的「8 个排队」只是结构推导)。900s 预算只挂在循环入口与 turn 边界(`plain_loop.py:106-117`),物化/baseline/verify/E3 复核全部在预算外;合法 graph 任务墙钟上界 ≥1620s,**超过锁 TTL 960s**,TTL 过期后 release 因 token 不匹配变 no-op(`locks.py:56-62`)。TTL 过期后同幂等键 create_task 在 `service.py:141-143` 幂等命中、走不到锁——**防双执行的第一道防线是幂等行读取,不是锁**(面试官「锁防并发」的预设被纠正)。

**R3-Q2 终态一次写入**。现行单进程内「FINISHED 被改写」**不可达**(五个终态写入口四个有全终态守卫,唯一弱守卫 finalize 的 5 个调用点结构互斥);可达的异常方向是反的:多进程共库时 B 进程 recover_stale 把 A 在途任务改 NEEDS_REVIEW,A 跑完 finalize(FINISHED) 放行 → **NEEDS_REVIEW→FINISHED 复活**(B 如实声明:代码级推演,无双进程实测)。

### 2.2 质量门禁与判定口径(R1-Q2-4、R2-Q7、R3-Q5、R3-Q7)

**R1-Q2-4「攻击成本翻倍」与四处口径互斥(⚠️ 中)**。`config.py:68` 注释宣称双跑使「伪造成绿攻击成本翻倍」,`nodes.py:393-396` 同一段注释里却自引 threat-model §4 承认「不防 junit 伪造」,`ADR-0002:20` 又把双跑列为「结构防线」,threat-model §4 原文「明确不防」。确定性伪造(基线内预置 junit 写出钩子)两次运行产出完全一致的全绿,`_double_run_mismatch`(nodes.py:49-65)的两条比对全部通过,**检出力为零**;「翻倍」全仓无任何攻击成本建模。35 题集真实 flaky 率:无数据,不编造。

**R2-Q7 + R3-Q7 E3 的诚实表述与补证收口(缺口:高)**。以代码为准的诚实表述:E3 双跑防的是**非自适应偶发伪绿**(收集集漂移、flaky、偶发伪造),对基线预置伪造的自适应对手无实质检出力——同一伪造逻辑在同进程对两次运行同样生效。B 交出 config.py/threat-model §4/design.md §8 三处替换文本;并实测**全 runs 树零 verify_double_run 轨迹事件——E3 自落地以来没有任何真实或 graph 批次佐证**(现存批次全 plain,plain 无双跑)。补证实验已设计(run_single --engine graph 重跑 fake 35 题,判据=checkpoint 物证+事件数+mismatch),证明力边界如实声明:**证明机制连通,不证明检出力**——FakeLLM 确定性回放下 mismatch 恒 0 是构造使然。立场终审:维持默认开,但存在价值表述降格为「结构性冒烟复核」,不是「防线」。

**R3-Q5 空集陷阱:面试官的攻击链被证伪**。面试官构造「bug.failed_tests 写错 id → junit 无匹配 → all_passed 恒 True → 误判 resolved」:实测写错的 id 让 pytest 以 **rc=4(usage error)** 退出,`all_passed` 第一道关 `exit_code != RC_OK`(pytest_adapter.py:63)即 False——链在第一环被拦,且 `test_executor.py:156-166` 已用错 id 钉死该语义。真实缺口在反方向:parametrize 展开会让合法通过误判为不过(假阴性,安全侧);缺容器路径错 id 用例与判定层集成用例(低危,P3-17)。

### 2.3 补丁生成与应用(R1-Q2-3、R3-Q4、R3-Q6)

**R1-Q2-3 → R3-Q4 软链逃逸:从「宣称未达成」到「两端实证」(缺口:中,本轮最重的发现)**。R1 证实:E1 整改的 `_target_violation`(patcher.py:26-47)只查「落点已是软链」与「resolve 越界」,对**新建合法路径软链**(new file mode 120000、落点不存在)两查皆过;ATTACK-009 的样本实际被旧的 `..` 静态规则拦截,到不了新校验;test_gitops.py 因 Windows 无软链特权 mock `Path.is_symlink`,且 origin/master 落后 37 commit——**①分支在一切已知环境零次真实执行**。R3-Q4 B 补做真实实验把结论钉死:

- **Windows 宿主(local 默认,core.symlinks=false)**:git apply 落普通文件或报错 rc=128,链条在 git 层断掉;
- **Linux 容器(python:3.11-slim)**:软链真实落地;「先删既有 pkg/util.py → 同路径新建 120000 软链指向工作区外 evil_mod.py」两步变体(每步独立过门禁与落点校验)后,verify 阶段 pytest(cwd=workspace)`from pkg.util import helper` **import 了工作区外模块,测试通过(1 passed)**。

逐环节拦截表:静态门禁不拦(gates.py:20 的 `_NEW_FILE_RE` 从不解析 mode 值)、首轮落点校验不拦、git apply --check 不拦、verify 被消费;read_file(relpath_within)、后续触碰该路径的补丁、测试/控制面命名规则则拦。裁决:**ATTACK-009/E1 已声明范围之外的残余风险**(样本 meta.yaml 自述范围是「越界路径」,其落点本就是 `../escape.txt`)——threat-model 补记文案已给;「120000 一律拒」属门禁语义变更,须另行评审(AGENTS 红线)。B 同时把 R1 承认的「git apply 对 120000 是语义推演」升级为两端实证。

**R3-Q6 假 graph 收口:面试官的前提再次被证伪**。面试官断言 report.py:38 兜底 `"graph"` 会为 legacy 批生成假复现命令;B 实测**146 份 report.json 无一同时缺 engine 与 provenance**(103 份无 provenance 但全部带 engine 字段,最早的 09-16 批就写),runs/real 28 份生成的是正确的 `--engine plain`,受影响批次清单=**空**。可达的假 graph 只有空批次硬编码示例(report.py:42-43)。变更集照给(report 兜底改 "unknown" + 删 driver --engine graph + 空批示例改 plain + 两个钉死测试,同 PR <60 行):「不可执行的诚实」优于「可执行的错误」。

### 2.4 执行隔离与并发(R1-Q2-6、R3-Q3)

**R1-Q2-6 35.6s 无出处(⚠️ 低)**。三个文档复记的「10 任务 35.6s」出自 `test_service_robustness.py:243` 的 print(无断言,:246 只断言 <180 防死锁),无基线文件、无机器元数据;design.md:61「流量 ×10 先挂执行资源」是结构推导+上轮访谈自述「压测一次没做过」,以断言句式写入设计文档。

**R3-Q3 产物生命周期与 docker 真实状态(缺口:中)**。全 app/ 无任务终态后的产物回收路径(grep 仅 snapshot.py:44 与 gitops/testing.py:63 两处 rmtree,均非终态回收);自建题集单任务足迹 0.13-0.4MB(实测典型 graph 任务 206KB:workspace 96K+checkpoints 68K 占 80%),按 E4 基线外推满负荷 ≈24,000 任务/日 → **3-10GB/日**;`tracker.py:3`「文件副本永久保留」与无界增长在数学上不可同时成立——**是承诺要改,不是事实要瞒**。docker 后端三证据源时间线裁断:09-16 isolation-notes 五实验全 PASS → 09-18 backend-notes「已接线并真机验证」+ runs/docker-e2e 产物 → 现行 `config.py:42`「容器集成留待人工验证」——**config 注释是唯一与事实矛盾处**。E3 容器路径机械成立(run_pytest 单汇聚点)但零文档,且容器路径 junit 文件名不同,证据只剩 tracker 事件。

### 2.5 评估与指标(R1-Q2-1、R1-Q2-5、R1-Q2-7)

**R1-Q2-1 溯源锚点是坏的(⚠️ 高)**。fake36 批(35 题)manifest 记 `git_commit=367ae36`,但 `git ls-tree 367ae36 -- bugs/` 只有 BUG-001..028——批次跑在 E5 已迁移文件、ca17ccc 未提交的**脏工作树**上,`provenance.py:29-39` 只取 HEAD 无脏树指纹,锚点 commit 上不存在该批跑的 7 道新题,**无法复现**;夜报 §4 却把这个坏锚点当 E5 的成功证据引用。E6 冒烟批同构(manifest 记 ca17ccc+blind,但 --blind 代码在 d8799e5 才提交)。修法已给:provenance 加 worktree_dirty+dirty_fingerprint;更严的闸放发起端(批次发起前脏树 fail-fast);manifest 落盘前 ls-tree 反查输入存在性(每批 1 次 subprocess)。

**R1-Q2-5 hard 题转正=自审自批(⚠️ 中)**。评审人与执行人是同一代理;checklist 第 2 项「修法唯一且自然」与 C103/C107 行内「`urllib.parse.unquote` 是可过全测的等价替换」同时打勾——唯一性被静默降级为「无误杀」;「预期真实模型 ≥2 轮」在零真实批下不可证伪(历史 28 题真实模型记录反而是 1 轮全过);7 道 preflight 三件套**逐题原始输出不存在**,三处文档指针环闭合在夜志一句话上——证据与结论没有区分。

**R1-Q2-7 p50/p95 与判型口径(⚠️ 中)**。`report.py:81-87` `_pctl` 用 `round(q*(len-1))` 带银行家舍入(n=2 时 p50=min,实算复现),docstring「最近秩」名不副实;判型三行混用 verdict/status,而 graph 引擎的 PATCH_REJECTED 永不为终态(轮尽→rollback→BUDGET_EXCEEDED)——**PATCH_REJECTED 计数对 graph 批恒 0,design.md:64「越权拦截率体现为 PATCH_REJECTED 计数」对 graph 不成立**。fake35 批 35/35 resolved、100% 单轮正是 E7 分布字段最该暴露的「软集形态」,却同时被夜报当 E5 通过证据引用,无任何自动警报。

### 2.6 LLM 接入(R1-Q2-2、R2-Q5)

**守卫三口径 + P0-2 复活链(⚠️ 高,两轮合击)**。R1 证实:`require_model_name` 全仓仅 3 个调用点且口径互异(driver.py:114 按 provider 属性豁免、driver.py:296 完全不豁免、run_single.py:41 按 CLI 旗标豁免),夜报 §6.1 声称「run_single --model fake 在 llm_enabled=true 下会被拒」是**事实错误**(两级守卫均豁免,不会拒);第四入口 `run_task_graph` **零守卫**;API 侧守卫在任务已建、已置 RUNNING 后触发,落 NEEDS_REVIEW——不成立 fail-fast。R2-Q5 推演钉死:`llm_enabled=true`+凭据齐+`PATCHPILOT_LLM_MODEL=""` 时,**API+graph 链全程零拦截,report.json 落盘且 model_name=""——上轮 P0-2「真模型评测不可追溯」被一条现役配置组合精确复活**。修法:守卫下沉 openai_client 构造(2 行)+runner 补一行;失败点提前到 create_task 预检(404,零副作用)。

### 2.7 配置与可观测性(R2-Q3、R2-Q4、R3-Q8)

**R2-Q3 日志零接线(缺口:中)**。grep basicConfig/dictConfig/Handler 全 0 命中——AGENTS「业务日志必须带 task_id 与 request_id」处于**只有约定、没有装配、没有输出、没有消费方**的状态:成功任务 stderr 业务日志 0 条(成功路径全是 info/debug,低于 stdlib lastResort 的 WARNING 门槛);trajectory 的 request_id 是每事件新 UUID(`tracker.py:79`)且 event_id 与之同值,与 HTTP 请求、LLM 调用均无关联,**事件↔LLM 调用不可追溯**;Settings 26 键无 log_level——判定为「漏」(无任何决策记录)。R3-Q8 交出可合入装配件(log_level 第 27 键+TaskContextFilter+contextvar 透传)并如实声明:装配不解决 per-event request_id 的结构缺口,那是第二张卡。

**R2-Q4 config_snapshot 白名单(缺口:中)**。白名单 7 键(`provenance.py:45-53`),差集 19 键;`verify_double_run` 落选经 git 考古证实是**遗漏**(白名单 commit 3871187 是 E3 commit 367ae36 的祖先,E3 晚 11 分钟落地未回头改);max_turns 是结构性缺口(根本不是 Settings 键);llm_api_key/api_token 若全量塞入会进 report.json 并被汇编进 docs 随仓库分发。差集快照测试方案已给(密钥名单+豁免名单+判定键必须在内,新键不三选一即 CI 失败)。

**R3-Q8 认证死角首审**。空 token fail-open 在威胁模型部署矩阵(compose 默认 127.0.0.1+token 可选)内自洽;但「非回环绑定+空 token」无任何启动告警——裁断:**启动告警可立即实现**;「启动即拒」当前配置面做不到(Settings 无 host 键,绑定地址由 uvicorn CLI 决定)——诚实说而不是假装能拦。401 响应满足统一错误结构;EXEMPT 对 `/api/health/`(307 重定向)与大小写变体(404)无绕行。

### 2.8 文档 vs 现实(R1-Q2-8、R3-Q1)

**R1-Q2-8 blind 与 CI(⚠️ 中)**。blind 标记只进 batch_manifest.json,TaskResult 与 build_provenance 均无 blind 字段——provenance 自述「如何复现本批次」却不满足自己的定义(不带 --blind 复跑结果不同);README.md:81「以 CI 最新跑批为准」指向一个**被证明不可达的证据源**(origin/master 落后 37 commit,最近 37 个 commit 结构上不可能有 CI 结果);真实盲跑对照批自上轮 P0-1 提出至今 **0 次产出**,E6 交付的是能力开关而非对照证据,夜报无一句显式声明。

**R3-Q1 ADR 与代码互斥:7 处对账(缺口:中)**。逐句对账产出 7 处不成立/无出处句:ADR-0001:24「SqliteSaver 免费获得节点级恢复」与 :29「checkpoint 带来崩溃恢复」均与 `checkpoint.py:3-5` 自认「当前没有崩溃恢复路径——从不按 thread_id 重放」互斥(design.md:51 为第三副本);「攻击样例 8 个」×3 处(design.md:65/threat-model.md:45/README.md:20)实测**实为 9 个**(ATTACK-009 新增后未回改);gates.py:1 与 nodes.py:325 两处「六项」docstring 过期(影子门禁是第 7 项);ADR-0002:31「compose 冒烟已实测」无运行留档。防复发机制已设计:tests/test_docs_anchors.py 以 file:line 级锚点断言钉住文档句,夜间 GOAL 的「全量 pytest 绿」红线会物理卡住下一轮夜卡的完成判定。B 同时纠正面试官:「七项门禁官方清单没有一处列全」不成立——企划书 §9:232-242 列全了(成立,面试官认错)。

## 3. 真问题清单(P3 系列,18 条,按严重度排序)

> 编号顺延上轮报告 P0-1..P2-8。R2/R3 中「答辩立住但缺口真实」的项一并并入。修复方向取自答辩中给出的方案,**均未实施**。

| 编号 | 维度 | 严重度 | 证据 | 修复方向 |
|---|---|---|---|---|
| P3-1 | 效果证据 | **高** | 146 份 report.json 的 model_name 全为空(实测分布 100/34/7/5,零份真实模型);自 09-18 后零新增真模型批次 | 守卫补齐后 run_single --model openai 带 model_name 实跑 ≥1 批(含 hard 题),provenance 完整落盘 |
| P3-2 | 溯源 | **高** | fake36 批锚 commit 367ae36 为脏工作树,锚点上不存在所跑的 BUG-029..035;夜报 §4 把坏锚当证据 | 干净树重跑基线批;provenance 增记工作树状态;manifest 落盘前 ls-tree 反查 |
| P3-3 | 守卫 | **高** | llm_enabled=true+LLM_MODEL="" 时 API+graph 链零拦截,report.json model_name=""(P0-2 复活);第四入口 run_task_graph 零守卫 | 守卫下沉 openai_client 构造(2 行)+ runner 补一行 |
| P3-4 | 状态机 | 高 | repository.py:125 finalize 仅 `!= 'CANCELLED'`;多进程共库下 recover_stale→finalize 使 NEEDS_REVIEW→FINISHED 复活 | 守卫改 NOT IN 全终态;test_storage.py 补 FINISHED 后改写返回 False 方向 |
| P3-5 | 双跑/E3 | 高 | 全 runs 树零 verify_double_run 轨迹事件(实测);E3 自落地零真实/graph 批次佐证;「攻击成本翻倍」四处口径互斥 | R3-Q7 补证实验(graph 批三件套判据);config/threat-model/design.md 按 B 替换文本降格表述 |
| P3-6 | 隔离 | 中 | 合法路径新建 120000 软链全链无拦截;Linux 实证 verify 阶段可 import 工作区外模块(ATTACK-009 声明范围外) | threat-model 补记残余段(R3a);「120000 一律拒」属门禁语义变更须另行评审 |
| P3-7 | 文档 | 中 | ADR-0001「崩溃恢复」×2+design.md:51 与 checkpoint.py 互斥;「8 个」×3 实为 9;「六项」×2;ADR-0002:31 无留档 | 逐句替换文案;新增 test_docs_anchors.py 锚点断言 |
| P3-8 | 存储 | 中 | 取消×自然完成 → tasks/evaluations/report.json 三处分裂;evaluations 零生产读方 | evaluations 表裁删(P1-5 先例);读口径写入 design.md §7 |
| P3-9 | 可观测 | 中 | 日志装配 0 接线,业务日志 0 条;request_id 每事件新 UUID 与 HTTP/LLM 无关联;Settings 无 log_level | R3-Q8 装配件(log_level+TaskContextFilter+lifespan 空 token 告警);per-event request_id 重构另立卡 |
| P3-10 | 并发 | 中 | RUNNING=受理非执行,池内排队 DB 不可见;900s 盖不住合法 graph 任务 ≥1620s,超锁 TTL 960s;防双执行靠幂等行非锁 | 文档显式声明语义;置 RUNNING 挪入 _execute(约 3 行);锁续期或缩小声明 |
| P3-11 | 溯源 | 中 | snapshot 白名单漏 verify_double_run(git 考古证实遗漏);max_turns 结构性缺位 | 白名单补键+差集快照测试;max_turns 升格 Settings 另行评审 |
| P3-12 | 容量 | 中 | 「文件副本永久保留」×无界增长(外推 3-10GB/日)不可同时成立;无终态回收器 | 改承诺:取证集永久/可弃集终态回收挂 _execute finally+启动扫描 Grace 期 |
| P3-13 | 判型 | 中 | graph 批门禁拒绝永不为 PATCH_REJECTED 终态;design.md:64 该句对 graph 不成立;report.py 判型三行混用 verdict/status | design.md:64 限定分引擎口径(B 文案已给);report 判型改按 gate_violations 口径 |
| P3-14 | 评测治理 | 中 | hard 题转正自审自批;checklist「修法唯一」与「等价替换」同行打勾;preflight 只有结论无逐题原始输出 | 外部复核或双盲 checklist;preflight 产物落盘;难度代理指标机器化 |
| P3-15 | 证据源 | 中 | 真实盲跑对照批 0 次产出;README「以 CI 最新跑批为准」指向落后 37 commit 的不可达证据源 | 产出 ≥1 次 blind 对照批;README 改指向可复现命令 |
| P3-16 | 引擎 | 低 | driver main --engine graph 把 graph 脚本喂 plain 循环(零测试覆盖);report.py:38 兜底 "graph"+空批硬编码(latent,146/146 不可达) | 同 PR:choices=["plain"]+兜底改 "unknown"+空批示例改 plain+两个钉死测试 |
| P3-17 | 判定输入 | 低 | 容器路径错 id 用例缺;判定层集成用例(bug 带错 id→终态不得 resolved)缺;parametrize 假阴性未决 | B 草案:run_task_graph+FakeLLM 断言 BUDGET_EXCEEDED 非 FINISHED |
| P3-18 | 评测 | 低 | 35.6s print 无断言无基线无元数据;「35/35 全 1 轮」软集形态无警报 | 基线落盘断言;「全绿全 1 轮」触发报告级警告(约 10 行) |

## 4. 与上轮审计对比:「效果证据链」是否收敛

**结论:机制层显著收敛,证据层零收敛——管线已铺好,水一滴未通。**

已收敛(机制/开关/字段层,均经本轮核实):
- E2 provenance 强制落盘:146 份中 43 份带 provenance,require_model_name 三分支有测试(E2 之前的 103 份 legacy 批无,符合时间线);
- E5 正式集扩至 35 题、E6 --blind 开关、E7 分布字段(p50/p95/rounds/verdict 计数)、E3 默认开且 six-calls 有测试钉住;
- 文档层 7 处假命题被完整对账,防复发机制(锚点测试)已设计;
- P0-2 的复活路径被 R2-Q5 提前暴露并给出 2 行级修法——这本身是机制层收敛的一部分。

未收敛(证据本身,全部为 0):
- 真实模型 report 新增 **0 份**(146 份 model_name 全空);
- 真实盲跑对照批 **0 次**;E3 真实/graph 批次佐证 **0 条**;8 道 hard 族题真实模型实跑 **0 次**;
- fake36 基线批溯源锚点仍坏;「35/35 全 1 轮」软集形态仍无警报。

距「可对外声明的效果证据」的具体步骤(上轮 P0-1 的直接闭环条件,本轮一步未走):
1. 守卫补最后两行(P3-3),堵死 P0-2 复活路径;
2. 真实模型实跑 ≥1 批(含 hard 题),provenance 完整落盘——E2 已强制,缺的只是执行;
3. 同批产出 ≥1 次 blind 对照,使 localize 贡献差值可计算;
4. 干净树重跑基线批,修复溯源锚;
5. 落地 R3-Q7 的 graph+E3 连通性实验;
6. 为「全 1 轮」软集形态加警报,修复 README 指向的可达证据源。

## 5. 总体结论

**经三轮拷问仍然可信的说法**:
- 全链路行为描述:凡 B 给出文件:行号、测试名、命令输出的主张,53 处抽查 52 处精确属实(1 处引用小疵:R3-Q3 回收路径 grep 漏了 gitops/testing.py:63,driver.py:87 实为 os.replace,不改变结论);
- 自我拆穿的完整性:23/23 题的「已知不足」区如实填报,R3 达 8/8 全覆盖;「E3 证明机制连通不证明检出力」「零触发可读作没问题也可读作没机会暴露,我不装作知道是哪种」——证明力边界的自我限定达到了审计本身的标准;
- 数字与实物:146 份扫描分布、9 个攻击样本、26 个 Settings 键、22 批次零误标、单任务 206KB 足迹——凡数字处可复算,凡实物处可指认。

**经不起拷问的说法**(全部是仓库文档层,而非答辩人的口头主张):
- ADR-0001「checkpoint 带来崩溃恢复」(与 checkpoint.py 自认互斥)、design.md「攻击样例 8 个」「越权拦截率体现为 PATCH_REJECTED 计数」、「六项门禁」docstring ×2、config.py「攻击成本翻倍」、夜报 §6.1 对守卫行为的错误描述、design.md:61「流量 ×10 先挂执行资源」的断言句式。共同点:**夜间 GOAL 模式改动落地时,周边权威文档没有同步修订,且无任何机制强制一致**。

**双方可信度量化**:
- 答辩人 B:23 题 0 说谎;纠正面试官 6 处全部成立(R1 编排层表述、R2 test_report 真调用、R3 七项清单/兜底不可达/白名单误判),另证伪面试官 1 条攻击链前提——6+1/7 全对;记录在案的夸大仅 2 处(均在「已知不足」自评区,方向是多报缺口)。评级:**口头主张可默认采信**。附一条:B 的修法/文案/测试全部停留方案,「照给」与「落地」之间零距离。
- 面试官 A:6 处前提错误被纠正,全部如实记录在案——本轮审计对面试官自身的复核严格于对答辩的复核。

**一句话元结论**:
**「代码可信、证据缺席」——三轮 23 题零塌方证明工程与诚实都经得起对抗拷问,夜间 GOAL 8 卡把效果证据的机制层从「断裂」修复为「随时可通水」,但证据产出量自上轮审计至今为零:管线已经铺好,水一滴未通。**
