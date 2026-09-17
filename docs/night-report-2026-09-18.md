# PatchPilot 夜间开发晨会报告(2026-09-18)

> 执行方式:GOAL 模式,任务唯一来源《docs/夜间GOAL模式开发SPEC.md》。
> 夜间日志:`runs/night-log-2026-09-18.md`(逐卡记录改动文件与测试计划,gitignored 不入库)。

## 1. 每卡状态

| 卡 | 内容 | 状态 | commit |
|---|---|---|---|
| N1 | API Bearer 鉴权(api_token,空=关闭) | ✅ 完成 | `e8200b5` |
| N2a | prompt/completion token 明细贯通 | ✅ 完成 | `4e96ac9` |
| N2b | 成本核算(价目表+SQLite migration+报告) | ✅ 完成 | `6517984` |
| N3 | cancel 协作式中断(turn 边界生效) | ✅ 完成 | `1d4b000` |
| N4a | 任意仓库任务接入(repo_path+issue) | ✅ 完成 | `a55add6` |
| N4b | 自定义仓库任务 e2e 与门禁攻击用例 | ✅ 完成 | `746a031` |
| N5(选做) | 8 道候选 Bug 题与验证脚本 | ✅ 完成 | `9427c64` |
| N6(选做) | 执行后端分发骨架(docker 探测通过) | ✅ 完成 | `eba9676` |

无 `[partial]`,无跳过卡。N6 开工探测 `docker info` 成功(Server 29.6.2)。

## 2. commit 清单(`git log --oneline c35d5da..HEAD`)

```
eba9676 feat(executor): 执行后端分发骨架(docker 集成留待人工验证)
9427c64 feat(bugs): 8 道候选题与验证脚本(待人工审题)
746a031 test(api): 自定义仓库任务 e2e 与门禁攻击用例
a55add6 feat(api): 任意仓库任务接入(repo_path+issue)
1d4b000 feat(api): cancel 协作式中断(turn 边界生效)
6517984 feat(evals): 任务成本核算(价目表+SQLite migration+报告)
4e96ac9 feat(llm): prompt/completion token 明细贯通到 report
e8200b5 feat(api): 可选 Bearer Token 鉴权(api_token,空=关闭)
71d67c9 docs(spec): 夜间 GOAL 模式自动开发 SPEC(N1-N6 任务卡与红线)
```

(夜间实际开工基线是 `71d67c9`——SPEC 文档本身提交后的 HEAD;SPEC 正文写的 `c35d5da`
在其前一个提交,129 个测试的基线状态两者一致。)

## 3. 测试数变化

**129 → 170(只增不减,+41)**;收工时 `ruff check . && ruff format --check . && pytest -q`
全绿(170 passed)。期间未 skip/删除/改写任何既有测试。

新增测试文件:tests/test_auth.py(6)、tests/test_pricing.py(8)、tests/test_cancellation.py(4)、
tests/test_custom_task.py(11,含参数化攻击用例)、tests/test_backend.py(7);
既有文件追加:tests/test_llm.py(+3)、tests/test_plain_loop.py(+1)、tests/test_driver.py(+1)。

## 4. 新增/修改文件清单

新增:`app/api/auth.py`、`app/api/cancellation.py`、`app/evals/pricing.py`、
`app/executor/backend.py`、`scripts/validate_candidate.py`、`docs/docker-backend-notes.md`、
`bugs_candidates/CAND-001..008/`(每题 7 个文件,共 56 个)、上述 5 个新测试文件。

修改:`app/config.py`(api_token/price_overrides/execution_backend)、`app/errors.py`(+TaskCancelled)、
`app/api/{app,routes,schemas,service,report}.py`、`app/storage/{db,repository}.py`(migration+成本列)、
`app/llm/{base,fake,openai_client}.py`、`app/graph/{plain_loop,nodes,runner,state}.py`、
`app/evals/{driver,bugset}.py`、`tests/{test_llm,test_plain_loop,test_driver}.py`(仅追加)。

### 建议人工 review 的 diff(按改动量/风险排序,前 3)

1. **`app/api/service.py`(1d4b000 + a55add6 + 6517984,累计约 ±90 行)** ——
   改动最集中:cancel 注册/注销接线、自定义任务分支与内存回放脚本透传、成本落库。
   特别看一处**落盘顺序调整**:终态回写从"先改状态后 persist"改为"先 persist 后改状态"
   (原顺序下轮询方看到终态时轨迹可能尚未入库,N3 全量跑时真实命中过一次
   `test_full_task_lifecycle` 偶发失败,已修复并连跑两轮全绿确认)。CANCELLED 保护
   改为终态写入前一刻重读,语义不变。
2. **`app/graph/{plain_loop,nodes,runner,state}.py`(4e96ac9 + 1d4b000)** ——
   全部是禁区周边的"加参数/加字段/加 except 分支"式扩展:cancel_event 贯通、
   token 明细进 TaskState(nodes 返回 schema 外键会被 LangGraph 拒绝,故 state.py 加了
   tokens_prompt/tokens_completion 两字段)、localize/propose 加 `except TaskCancelled: raise`
   防止取消被吞成 NEEDS_REVIEW。请确认扩展幅度符合预期。
3. **`app/api/schemas.py`(a55add6)** —— TaskCreateIn 新增 6 个可选字段 +
   model_validator(恰好一个来源;repo_path 时必填)。校验失败的 404/422 语义
   与 SPEC 约定的一致性值得人工过目。

## 5. 遗留问题与卡点

1. **鉴权实现方式**:FastAPI 0.141.1 中依赖的返回值不会短路请求(router 级依赖返回值
   被直接丢弃),401 统一错误结构用"自定义 `UnauthorizedError` + app 级 exception_handler"
   实现(`app/api/auth.py` + `app/api/app.py`)。升级 FastAPI 时回归 tests/test_auth.py。
2. **价目为约值**:`app/evals/pricing.py` 内置 4 个模型(报告已注明"约值"),
   `price_overrides` 指向的 JSON 解析失败时回落内置并打 warning(不抛异常)。
   不回填历史任务、不做计费(按 SPEC 非目标)。
3. **graph 引擎的 cancel 无专门单测**:API 集成测试覆盖 plain/driver 路径;
   graph 侧走 nodes 新增 except 分支 → runner 捕获,逻辑简单但未直接测试,建议白天补一条。
4. **N6 只到骨架**:`run_tests_by_backend` 未接入任何真实执行路径,接线点分析、
   两个候选方案与白天验证清单(Windows 挂载语法、junit 路径映射、镜像内依赖、
   超时杀树语义等)见 `docs/docker-backend-notes.md`。`docker_available()` 带 lru_cache,
   守护进程中途启停不会刷新。
5. **N5 候选题注记**:CAND-001/006 的缺陷本身就是 ruff B006/B023 命中的写法,
   为保持绿线在缺陷行加了 `# noqa: B006/B023` 注释(注释本身写明"标记的正是本题缺陷"),
   人工审题时请知悉。CAND-003/005/008 的失败用例均做过确定性验证
   (naive/aware TypeError、map 二次消费、0.29×100 截断)。
6. **working tree 遗留(非夜间产物)**:仓库根 4 个企划/计划文档在工作区被删除、
   `docs/` 下存在同名未跟踪文件(即一次未提交的目录搬移),另有未跟踪的 `.zcode/`。
   这些改动在夜跑开始前就存在,夜间未触碰;是否提交由人工决定。

## 6. 候选题清单(N5,均在 `bugs_candidates/`,与 `bugs/` 正式集隔离)

| 编号 | 类别 | 难度 | 缺陷形态 |
|---|---|---|---|
| CAND-001 | 可变默认参数 | simple | `def add_note(note, notes=[])` 共享列表 |
| CAND-002 | 状态共享 | simple | 类属性字典跨实例共享(Cart) |
| CAND-003 | 时区比较 | medium | naive/aware datetime 混用抛 TypeError |
| CAND-004 | 格式化注入 | medium | `str.format` 属性访问越权 + KeyError |
| CAND-005 | 生成器语义 | simple | 生成器被 sum 消耗后二次遍历为空 |
| CAND-006 | 闭包绑定 | medium | 循环内 lambda 迟到绑定 |
| CAND-007 | 顺序语义 | simple | `list(set(...))` 去重丢失首次出现顺序 |
| CAND-008 | 浮点精度 | medium | float 金额 `int(sum*100)` 截断差一分 |

8 道全部通过 `scripts/validate_candidate.py`(manifest 字段齐全、双回放脚本可解析、
failed 集逐条基线失败、regression 集基线全绿);额外验证:每道回放 diff 可 `git apply`
且修复后 failed 集转绿。缺陷形态与现有 20 题(异常处理/边界条件/类型错误/数据访问/跨文件定位)不重复。

## 7. 全局完成定义核对

- [x] 必做卡 N1/N2a/N2b/N3/N4a/N4b 全部完成(无降级);
- [x] 选做卡 N5/N6 完成;
- [x] 全量 `ruff check . && ruff format --check . && pytest -q` 绿(170 passed);
- [x] 夜-log(`runs/night-log-2026-09-18.md`)与晨会报告(本文)落盘;
- [x] 夜间产物全部已提交(8 个 feat/test commit;遗留的根目录文档搬移为夜跑前已存在的用户改动,见 §5.6);
- [x] 红线零违反:未设 `PATCHPILOT_LLM_ENABLED=true`、未发起真实 LLM 请求、未装新依赖、
      未 push、未改历史提交、未改 ci.yml/AGENTS.md/SPEC、未改 `tests/` 既有期望、
      未放宽任何门禁;禁区文件只做了 SPEC 明示的加参数/加字段/加 except 分支扩展。
