# 夜间 GOAL 模式晨会报告 · 2026-09-25 · 效果证据链专场

执行:无人值守代理 | 任务来源:`docs/夜间GOAL模式SPEC-2026-09-25-效果证据链.md`
基线:master `b6aee2c`,258 passed + 2 skipped | 全程 `PATCHPILOT_LLM_ENABLED=false` 前缀,零真实花费。

## 1. 每卡状态

| 卡 | 状态 | commit | 摘要 |
|---|---|---|---|
| E1 apply 侧软链逃逸 + ATTACK-009 | ✅ 完成 | `bd9e091` | patcher.py apply 前落点校验(①已存在软链→symlink_escape;②resolve 越界→path_escape);ATTACK-009 入库,攻击样例 8→9 |
| E2 评测溯源强制 + 批次清单 | ✅ 完成 | `3871187` | provenance 增 git_commit/config_snapshot(7 键白名单)/started_at;require_model_name fail-fast 落 run_task/run_single/run_batch;batch_manifest.json(8 字段) |
| E3 verify 双跑一致性复核 | ✅ 完成 | `367ae36` | verify_double_run 默认开;第一遍双绿才重跑比对,不一致→NEEDS_REVIEW(verify_mismatch)+双跑摘要入轨迹;成功路径 pytest 4→6 次 |
| E5 hard 候选题审题转正 | ✅ 完成(7/8) | `ca17ccc` | C101..C107→BUG-029..035 转正;C108 难度标定不符不转正;正式集 28→35;fake35 批次 35/35 resolved |
| E6 盲跑对照模式 | ✅ 完成 | `d8799e5` | --blind:issue 置固定占位,测试集原样;manifest 增 blind 标记;新增批次 CLI(python -m app.evals.driver);graph 层零改动 |
| E7 报告分布字段与口径刷新 | ✅ 完成 | `0360664` | 耗时 min/p50/p95/max、Token min/max、轮数分布 1/2/3+、判型计数;eval-report.md 用 fake35 批重生成;eval-report-real.md 文首补溯源缺失注记 |
| E4(选做) 并发冒烟与容量声明 | ✅ 完成 | `6f4f6d3` | task_max_workers 配置化(默认 2,向后兼容);10 CUSTOM 任务并发冒烟 35.6s 全 FINISHED;design.md §8 容量模型如实陈述 |
| E8(选做) 三篇 ADR + 数字治理 | ✅ 完成 | `f406777` | adr/0001 编排、0002 执行后端、0003 LLM 客户端(均含反方);README 删硬编码"210+ 用例";企划书/计划书加口径注记 |

跳过:无。降级:无。所有卡在时间盒内完成。

## 2. commit 清单(`git log --oneline b6aee2c..HEAD`)

```text
6f4f6d3 feat(api): 并发上限配置化 + 10 任务并发冒烟基线
f406777 docs(adr): 三篇选型 ADR + 测试数字口径治理
0360664 feat(evals): 报告分布字段(p50/p95/rounds/verdict 计数)+口径刷新
d8799e5 feat(evals): 盲跑对照模式(--blind,issue 置占位)
ca17ccc feat(bugs): hard 候选题审题转正(C101..C107→BUG-029..035,AI 审题记录入库;C108 难度不符留候选区)
367ae36 feat(graph): verify 双跑一致性复核(verify_double_run,默认开)
3871187 feat(evals): 评测溯源强制(git_commit/config 快照/fail-fast)+批次清单
bd9e091 fix(gitops): apply 前软链/路径逃逸校验(ATTACK-009)
46d2e66 docs(audit): 收编面试审计报告与夜间GOAL SPEC(本轮任务来源,内容零改动)
```

注:`46d2e66` 为满足 SPEC §5"working tree 干净"完成定义,将两份未跟踪的任务来源
文档**原样收编**(内容零改动,未被本任务声明的"禁改"范围禁止)。

## 3. 测试数变化

**258 passed + 2 skipped → 290 passed + 2 skipped**(+32,只增不减;skip 数不变 ✓)
全程无 skip/删除/改写既有测试;每卡 commit 前 ruff check + ruff format --check + pytest -q 全绿。

+32 构成:E1 +3(gitops 2 + attacks 专项 1);E2 +5;E3 +3;E5 +15(bugset 参数化 7 题×2 + 计数护栏 1);E6 +3;E7 +2;E4 +1。

## 4. E5 审题结论一览(转正 7/8)与 fake35 批次

| 题 | → | 结论 | 一行理由 |
|---|---|---|---|
| C101 | BUG-029 | 转正 | 两跳链定位(report→summary→aggregator),根因未泄底,Counter 等价修法无误杀 |
| C102 | BUG-030 | 转正 | 双文件强制(单修任一必失败),封顶/加运费修法唯一自然 |
| C103 | BUG-031 | 转正 | 对照定位需推导字节级解码规则(+字面+UTF-8 合并);urllib.unquote 为可过全测的等价替换(注明) |
| C104 | BUG-032 | 转正 | 单位换算根因在下游 units.py,issue 未泄底;修法唯一 |
| C105 | BUG-033 | 转正 | truthy-0 陷阱+阈值边界双缺陷;test_is_low_at_threshold 直测 rules 防绕过 |
| C106 | BUG-034 | 转正 | 双文件强制(不接线 escape_cell 三 failed 全红),修法唯一 |
| C107 | BUG-035 | 转正 | None falsy 陷阱+转义表缺口;html.escape 为等价替换无误杀(注明) |
| C108 | — | **不转正** | 难度标定不符:fast 版是 ref 的一行变体,`encode("ascii", errors="ignore")` 对照即得,预期真实模型 1 轮过,不满足 hard"≥2 轮"定义;留候选区建议重标 medium |

逐题证据:`bugs/candidates/review-2026-09-25.md`。preflight 三件套 7/7 在当期 master 复验通过。

**fake35 批次**(`runs/fake36-2026-09-25/`,SPEC 定死目录名;35/35 resolved;
batch_manifest 带 git_commit=367ae36):

| 分布 | 值 |
|---|---|
| verdict 计数 | resolved 35 · PATCH_REJECTED 0 · NEEDS_REVIEW 0 · 其他 0 |
| 轮数分布(1/2/3+) | 35 / 0 / 0 |
| 耗时 min/p50/p95/max | 6679 / 6876 / 7509 / 9752 ms |
| Token min/max | 133 / 314 |

注:此为 **fake 回放口径**(FakeLLM 无视消息内容,单轮 100%),只证平台闭环;
真实区分度数据须等"人工触发清单"的真模型批。

## 5. 新增/修改文件与建议人工 review 的 diff

规模:100 files changed,+1603/−42。新增:ATTACK-009 样例、BUG-029..035(迁移)、
candidates/review-2026-09-25.md、docs/adr/ 三篇、runs/ 夜志与三个评测批次产物(gitignore)。

**建议人工 review 前 3**(按改动敏感度排序):

1. **`app/graph/nodes.py` verify 分支 + `app/graph/builder.py`(367ae36)**——
   判定面结构性改动:double-run 触发条件、mismatch 判定(`_double_run_mismatch`)、
   NEEDS_REVIEW 路由出口。builder 加了一条 `"end": END` 边(SPEC 范围写"仅 nodes.py",
   这是路由终点必要配套,夜志已记)。
2. **`app/gitops/patcher.py`(bd9e091)**——apply 前落点校验在"必须掌握"区边界上,
   实现严格按 SPEC 定死的①②两分支;ATTACK-009 定位为"引擎级样例"(expected_layer:
   patcher)的分层决策建议复核。
3. **`app/evals/driver.py`(3871187 + d8799e5)**——run_task 内 fail-fast 的
   fake-replay 豁免(`provider != "fake-replay"`)是守卫语义的关键裁量:
   不豁免会把 service/run_single 的 fake 路径在 llm_enabled=true 实例上全炸断;
   run_batch/CLI 为新增面。

## 6. 遗留问题与卡点

1. **`.env` 的 `PATCHPILOT_LLM_ENABLED=true` 未改**(红线:任何卡不得改回/设为 true,
   也不得改 false——属用户配置)。本轮全部命令以前缀覆盖。**提醒**:E2 守卫上线后,
   该状态下不带前缀跑 fake 单题 CLI(`run_single --model fake`)会被拒
   (run_task 的 fake 豁免只覆盖 provider=fake-replay 的任务路径;run_single 的
   openai 分支不受影响)——建议晨会决定 .env 收口方式。
2. **`demo/run_dirty_ticket.py` 不满足 ruff format**(基线即如此;demo/ 非任务文件,
   不提交不删除不代为格式化)。全仓 format --check 除它外全绿。
3. **E1 分层注记**:静态门禁先于 patcher 拦截越界文本,ATTACK-009 经工具层到不了
   落点校验——故与 ATTACK-008 同列引擎级样例,由专项测试直接驱动 gitops 层。
   第一手"合法路径软链"(mode 120000、路径合法)不在 SPEC 定死的①②拦截范围;
   经由该软链的后续写入会被②(resolve 跟随)拦截,读侧由 paths.py 防护。
   如需"结构上不可能存在软链"的更强保证,需追加 blob 内容检查——超出本 SPEC,留议。
4. **真模型批是效果证据链的最后一环**(SPEC §4 人工触发清单,未代跑):
   36 题真模型重评(验 E2 溯源落盘)与 --blind 盲跑对照批(产出 localize 贡献第一份证据)。
5. docs/README.md 生成产物快照表中 eval-report 条目的口径描述已过时
   (仍指向 runs/m9 旧批次),属"生成产物描述"性质,未擅动,建议随下次真实批一并刷新。

## 7. 完成定义核对(SPEC §5)

- [x] 必做卡 E1/E2/E3/E5/E6/E7 全部完成,选做 E4/E8 亦完成;
- [x] 全量 `ruff check + ruff format --check + pytest -q` 绿(290+2skip);
- [x] night-log(`runs/night-log-2026-09-25.md`)与本报告落盘;
- [x] working tree 干净(`.idea/`、`demo/` 除外,按 SPEC 不提交不删除);
- [x] 禁区语义零改动:metrics.py 判定零触碰、gates.py/pytest_adapter 未改、
      tests 既有期望未改、未放宽任何门禁;禁 push ✓、ci.yml/AGENTS.md/SPEC 未改 ✓;
- [x] 零真实花费:全程 false 前缀,无任何真实 LLM 调用;SPEC §4 人工触发清单未代跑。
