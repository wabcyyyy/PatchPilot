# PatchPilot 整体复盘与整改记录 · 2026-10-07/08

> 体裁:全库复盘(发现 → 分级 → 整改 → 机器证据)的**整改结论**篇。
> 发现清单由三路深查(核心管线 / 评估体系 / 文档与外围)+ 本地复核产出,
> 分级为 P0/P1/P2,复盘后追加一轮 R 批(把"知道但没进首批计划"的项收掉)。
> 状态:**复盘发现的已知问题全部收口**,仅剩文末"仍开放"三项需要人的决策。

## 1. 结论(先说结果)

- 门禁:HEAD `3c669b6` 上 `ruff check .` + `ruff format --check .` 全绿;
  `pytest -q` **536 passed, 2 skipped, 0 failed**(16:33)。
  口径如实说明:那轮全量启动时 `3c669b6` 尚未提交,但其内容已在**工作树**里
  (工作树=跑批内容,提交在跑批之后),故结论对 HEAD 成立;HEAD 上另复跑
  `tests/test_executor.py` 20 passed。
  skip 的 2 例是 Redis 锁默认地址(6379)不可达,属既有口径(见 §4)。
- 提交:本复盘共产出 **21 个 commit**(基线 `9ca0c5b` → HEAD),分三批 + 一轮追加批。
- 关键核验:定位判据改动对 **439 份既有 report 离线重放,旧字段零漂移**;
  成本回填覆盖 **25 个付费 run,合计 $1.46**;
  Redis 锁路径经环境变量指向本机真实实例后 **6 passed**(该路径首次获得机器验证)。

## 2. 发现 → 整改对照

### P0 批(评估口径与成本)

| # | 复盘发现 | 处置 | commit |
|---|---|---|---|
| P0-1 | 价目表未收录实际评测模型 `deepseek-flash`,25 个付费 run 的 `cost_usd` 全 null | 按官方 peak 档(0.30/1.20 USD/1M,约值;off-peak 对半)补条目;一次性脚本 `runs/recompute_cost.py` 离线回填(不重跑任务) | `5beb4d1` |
| P0-2 | `metrics.py:116` 定位判据"相交即可",可被无关改动抬高 | **保留旧口径**(业界 file-level localization 的通用命中口径,历史数字不动),新增并存 `localized_strict`(触碰集⊆期望集),报告双列 + 口径说明(弱信号、p2p 抽样回归) | `5b2cdbb` |
| P0-3 | provenance 缺 `max_turns`,无温度/种子,n=1 无法重放 | 记录运行时实际 max_turns;temperature/seed 客户端从不设置 → 显式记 `None`(不猜) | `0b896e8` |

### P1 批(管线一致性与 API 承诺)

| # | 复盘发现 | 处置 | commit |
|---|---|---|---|
| P1-4 | 白名单拒绝参数化测试 id 的括号(`test_x[(1,2)]`),而 nodes 基线路径直跑绕过——同一测试"基线能跑、Agent 工具不能跑" | `SHELL_METACHARS` 移除 `( )`(shell=False 参数列表下无 shell 语义);`$` 保留故 `$(cmd)` 仍拦截 | `3864e38` |
| P1-5 | BudgetError 靠动态挂属性携带用量,上层 `getattr` 鸭子读——自家异常分层被绕开 | 改构造器具名可选字段;`plain_loop`/`driver`/`single_shot`/`nodes` 全部直读 | `c82dfab` + `a49d86f` |
| P1-6 | verify 段最多 4 次 pytest,单次各有 test_timeout 但从不查任务级 deadline,可整体越过 `task_timeout_seconds` | 入口/回归前/双跑前三处复查,超限以 `BUDGET_EXCEEDED` 终态收尾(与 localize/propose 同语义),部分结果随状态带回;`route_verify` 加 end 路由 | `a49d86f` |
| P1-7 | `executor/backend.py` 生产零调用且挂载语义与 `docker_runner` 互斥(audit-2026-09-19 P2-1 点名未收敛) | 整模块删除;`test_backend.py` 保留 Settings 校验与 run_pytest 路由的活测试 | `8eb442a` |
| P1-8 | 幂等键文档写"repo+commit+issue"而实际是 `bug_id\|engine\|model`;`request_id` 全链路为空(AGENTS 约定悬空);422 仍是 FastAPI 原生 `{"detail":[...]}` | 文档改实际口径;纯 ASGI 中间件按请求生成 request_id(回写 `X-Request-ID`,提交前在请求上下文捕获后随任务线程下发);补 `RequestValidationError` handler 输出统一结构;threat-model R6 补 redis 无密码条目与缓解建议 | `2200b27` |

### P2 批(小清理)

| 项 | 处置 | commit |
|---|---|---|
| 冒号误伤 POSIX 合法文件名(`weird:name.py`) | 只按 Windows 盘符形态(`^[A-Za-z]:$`)拒绝 | `838311c` |
| `nodes.py` 候选分支 `reserve//2` 为 0 时语义反转(0=不限制) | `max(reserve // 2, 1)`,让候选在首个 turn 边界即被预算拦下 | `838311c` |
| `SKIP_DIRS` 按 `path.parts` 匹配,workspace 落在 `node_modules/` 等目录下会全量误伤 | 改按仓库内相对段匹配 | `838311c` |
| `openai_client` 注释称"截断早失败"实际只 warning | **改行为对齐注释**:截断且仍有工具调用 → `TaskError`;纯文本截断维持告警 | `838311c` |
| `state.py` 注释称 `patch_fail_streak` 无写入方(实际分支合流节点有写) | 注释更正 | `838311c` |
| 企划书 §6 仍列五张表(代码已裁到三张);docs 索引漏收两篇;计划书/学习计划"五张表"表述 | 规范源改为实际三张并写裁撤沿革;索引补漏 | `b0722fd` |
| `demo/` 未跟踪且 format 不过(提交会破门禁);`.idea/` 未忽略 | 格式化后入库;`.idea/` 进 .gitignore | `ab960b2` |
| docker 报告目录 `0o755` 与容器 uid 1000 矛盾 | **只做事实验证不盲改**:真机 `docker run alpine` 实证 uid 1000 对 root 属主 0755 目录写入 `Permission denied`(exit=1);本机 Docker Desktop 形态下 30 个批次 junit 全部正常落盘 ⇒ 边界是"原生 Linux 宿主且服务用户 uid≠1000",写成部署注意事项 | `e89d851` |

### R 批(复盘后追加收口)

| # | 项 | 处置 | commit |
|---|---|---|---|
| R-1 | `latest_per_bug` 仅按 bug_id+mtime 去重,同目录混入两臂时静默丢一臂 | 去重键加 `provenance.arm`(缺省 agent);**引擎混批维持既有"每题取最新"披露口径不在此处动**。全目录扫描无 mixed-arm 题 ⇒ 零漂移 | `af543b9` |
| R-2 | AGENTS"全量类型标注"失实:`bug` 参数无标注、`TaskNodes.bug` 为 `Any` | `TYPE_CHECKING` 导入 `BugTask` 补真实标注(bugset 不依赖 graph,无循环;运行时零成本) | `69d0314` |
| R-3 | patcher 落点校验与 `git apply` 之间存在 TOCTOU 窗口 | 后置条件收口:apply 成功后对全部触碰路径复扫,违规即 `git apply -R` 反向还原 + 结构化拒绝;不再依赖"工具串行所以没人能换"的时序假设 | `4e7f578` |
| R-4 | 三处盲区无测试:local_runner 杀树/关管道兜底、checkpoint 初始化降级、graph 崩溃路径 finally 取证 | 补齐:管道永不释放时关管道回收不挂死;两条降级路径返回 None 不抛;崩溃收敛 NEEDS_REVIEW 仍落 diff.patch 与 report.json | `f9be6f7` |
| R-5 | Redis 锁两例长期 skip,曾误判为"机器没 Redis" | 真因:本机 redis 容器映射宿主 **6380 且带密码**;测试地址改 `PATCHPILOT_TEST_REDIS_URL` 可配(默认仍 6379,CI 零影响)。**6 passed** 首次覆盖该路径 | `9794d08` |
| R-6 | 消融两臂判定强度不对称(one_shot 臂走 plain 驱动器,无 verify 双跑) | **裁断为不补**:补双跑等于改动已登记消融变量之外的判定口径,历史两臂数字作废。写进 `swe-ablation-evidence` 局限 3,并规定后续新批必须两臂同引擎、或把判定强度差登记为第二个变量 | `478da47` |

### 过程性自我修正(不粉饰)

| commit | 内容 |
|---|---|
| `e1df6d3` | R-1/R-3 提交时只跑了 `ruff check` 没跑 `format --check`(且 `| tail` 把失败码吞成 0),补格式 |
| `3c669b6` | R-4 用例带入一个 RUF015 违规,补正(不重写已后续 3 个 commit 的历史) |
| `5e9e29f` | R-2 加导入使 `nodes.py` 文档锚点行号漂移(465→463→466),按 P3-7 设计重钉 |

## 3. 与批准计划的三处偏差(均有理由,已写入对应 commit message)

1. **成本回填范围扩大**:计划只写 swe-real 5 题,实为 25 题付费 run(消融臂与难题档同样缺成本),一并回填。
2. **verify 复查只做 deadline 不做 token**:verify 段无模型调用、不耗 token,任务级 token 预算由 propose/apply 段守卫。
3. **InMemoryLock 的 token 比对取消**:复核发现单实例内 `release(key)` 拿不到调用者身份(`_tokens` 会被最新 acquire 覆盖),加了也是无效护栏;真正的并发防线是 DB 幂等检查。改为 docstring 如实记录残余。

## 4. 机器证据索引(可复查)

| 断言 | 证据 |
|---|---|
| 全量测试绿 | `pytest -q` → `536 passed, 2 skipped, 0 failed in 993.14s`;数字对账:基线 524 →(−5 死代码用例 +12 P0/P1/P2 用例)= 531 →(+5 R 批用例)= 536 |
| 定位口径零漂移 | 用 `git show HEAD~:app/evals/metrics.py` 与工作区版本分别对 runs/ 下 **439 份 report** 重放 `load_run→annotate`,四派生字段 JSON 逐字节相同 |
| 成本回填 | `runs/recompute_cost.py --dry-run` 逐题打印,flask-5014 手算 $0.0357 对上;25 题合计 $1.46 |
| R-1 零漂移 | 全 runs 目录扫描 (bug_id → arm 集合),mixed-arm 计数为 0 |
| Redis 路径 | `PATCHPILOT_TEST_REDIS_URL="redis://:***@localhost:6380/0" pytest tests/test_locks.py` → 6 passed |
| docker uid 边界 | `docker run alpine`:uid 1000 对 root:root 0755 目录 `touch` → `Permission denied, exit=1` |
| 付费批次身份 | `runs/swe-real/*/report.json`:`model_name=deepseek-flash`、`model_provider=openai`、`.env` 的 `PATCHPILOT_LLM_BASE_URL=https://api.deepseek.com` |

## 5. 仍开放(需要决策,本次未动)

1. **LOCALIZE 瓶颈**:难题档两次复验后 sphinx-7590 仍 0 次 apply_patch。要继续推进需"抬预算 / 换模型"其中之一,都是付费且改变实验条件,按预登记的停手条件已停。
2. **gold 补丁相似度对比**:能回答"测试全绿≠修对"(pylint-6903 活样本),但需要新判定机制,属新任务卡而非整改。
3. **块协议锚定边界**:上下文重复的真实金补丁无法协议 round-trip(机械转换不可为、模型可自纠),该类题按既定裁断不入集。
