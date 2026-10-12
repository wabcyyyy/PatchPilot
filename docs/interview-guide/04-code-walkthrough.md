# 04 · 最重要的 20% 代码精读

> 选段标准：**面试会问的机制，恰好落在这几段里**。每段结构：代码 → 它在干什么 → 为什么这么写 → 会被怎么问 → 顺手讲的 Python 语法点。
> 行号以当前 HEAD 为准，可当场打开核对。

---

## 1. 请求入口：受理只做"决定要不要做"，不做"怎么做"

```python
# app/api/service.py（节选逻辑）
spec = build_task_spec(..., max_turns=DEFAULT_MAX_TURNS, ...)
idem_key = spec.task_spec_hash           # 任务身份 = 契约哈希
existing = repo.find_by_idem_key(idem_key)
if existing: return existing_row, False  # 幂等：同 spec 第二次进来不再开新任务
lock = build_lock(settings.redis_url)    # 默认 InMemoryLock
lock.acquire(f"task:{idem_key}", ttl=settings.task_timeout_seconds + 60)
```

**在干什么**：把"这次请求要不要执行"变成一个纯哈希比较 + 一把带 TTL 的锁。
**为什么**：客户端重试（网络抖动、用户多点一次）在无幂等的设计里就是**双份钱 + 双份工作区**。
**会被怎么问**："幂等键为什么用 spec 哈希而不是 request id？"→ 因为语义是"同样的输入就是同一个任务"，与客户端身份无关；代价是 `DEFAULT_MAX_TURNS=20` 竟然参与身份（`service.py` 硬编码、不是 Settings 项），改它会让历史任务的哈希漂移——这是我愿意承认的设计瑕疵。

`Python 语法点`：**`@lru_cache` + 冻结 dataclass**。`get_settings()` 带 `@lru_cache`（`config.py`）意味着 Settings 在进程内是单例，测试必须显式清缓存——`conftest.py` 的 `_fresh_settings` 就是 autouse 夹具在每条用例前后清一次，因为它曾经造成过跨用例泄漏。这是"装饰器改变函数语义 → 测试必须配合"的典型样本。

---

## 2. Agent 内循环：五道检查的顺序就是设计

```python
# app/graph/plain_loop.py（节选）
for turn_no in range(start_turn, max_turns + 1):
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled(f"cancelled at turn {turn_no} boundary")
    if deadline_epoch is not None and time.time() > deadline_epoch:
        raise _budget_error("task exceeded time budget ...", tokens_spent, ...)
    parked = _interaction_gate(turn_no)  # 等用户回答/纠偏
    if parked is not None:
        return parked
    messages, repaired = SessionState.repair_orphans(messages, recovery_policy)
```

```python
# app/graph/plain_loop.py（节选，资源账本）
call_id = ledger.begin_call(stage=state_label, round_no=round_no)
ledger.reserve(
    call_id=call_id,
    stage=state_label,
    round_no=round_no,
    request_tokens=estimate_request(messages, schemas).total_tokens,
)
```

**在干什么**：每回合开头按固定顺序过 取消 → 墙钟 → 交互 → 孤儿配对 → 保护区容量 → 压缩 → 阶段份额 → 任务账本，然后才发请求。
**为什么这个顺序**：
- 取消和截止是"必须立刻停"的，放最前；
- 交互门放在配对**之前**，因为恢复后要"从同一处重新进入，恰好一次"（代码注释明写）；
- `repair_orphans` 放在唯一发送点之前——注释解释了原因：严格端点遇到"assistant 声明了 tool_call 但没有回执"会直接 400，整题崩成 NEEDS_REVIEW；如果指望四个回填分支各自记得配对，就一定会有分支漏掉。
- `ledger.reserve` 的注释是最值钱的一句："**检查与登记在同一临界区完成**，比较基数是已用 + 全部在途预留 + 本次预留。此前只看已用，两个并发请求各自都装得下就都发出去了，现金/额度上限形同一半。"→ 这是 **TOCTOU 在预算上的版本**，也是"我先只查再扣"的竞态修复。

**会被怎么问**："两个并发请求同时通过预算检查怎么办？"→ 指着这段说"我踩过，现在是原子预留 + 在途计入"。
`Python 语法点`：**异常携带业务数据**。`BudgetError` 带 `tokens_spent/turns/last_content`（`errors.py`），`ModelTransportError` 带 usage 信封——因为"钱已经花了"这个事实不能因为抛异常就丢账。这是 Python 里用异常做**结构化返回**而非纯控制流的正确姿势（也解释了为什么补丁应用失败不是异常而是 `PatchApplyResult`）。

---

## 3. 工具统一入口：可见即可执行，异常一律收敛

```python
# app/tools/registry.py（节选）
spec = REGISTRY.get(name)
if spec is None:
    result = ToolResult.fail(f"unknown tool: {name}")
else:
    ctx.tracker.record(tool="tool_started", ...)          # 执行之前先广播
    try:
        bound = inspect.signature(spec.handler).bind(ctx, **args)   # ← 关键
        result = spec.handler(*bound.args, **bound.kwargs)
    except TypeError as exc:
        result = ToolResult.fail(f"bad arguments for {name}: {exc}")
    except Exception as exc:
        result = ToolResult.fail(f"{type(exc).__name__}: {exc}")
```

**在干什么**：用 `inspect.signature(...).bind()` 做参数校验；只有"绑定失败"才叫 `bad arguments`；其他异常统一变成 `ToolResult` 回填给模型；工具名前必须发一条 `tool_started`。
**为什么这么写**（注释里就是整改记录）："此前 `except TypeError` 会把 handler 函数体内的业务 TypeError 也误报成参数错误"——**异常类型复用导致的误诊**，这是 AI 生成代码里极其常见的缺陷，能讲出来就是审阅能力的证据。
`tool_started` 的价值："SSE 的 tool_started 与轨迹共享同一条事实（没有第二条进度来源）；崩溃在 handler 中途时，事件流上'已开始、无结果'与孤儿 tool_call 的恢复语义对得上。"→ **一条事实只有一个来源**，这是可观测性设计而不是加日志。
**会被怎么问**："工具抛异常会整个任务挂掉吗？"→ "不会，工具异常收敛成一次失败的回执喂回模型，由它决定重试还是换路；但只有工具层的异常收敛，执行器/门禁的异常是分层上抛的。"

`Python 语法点`：
- **`*` 强制关键字参数**：`def execute(ctx, name, args, *, round_no=0, state="-")` —— 防止位置参数误传；
- **`@property` 把判定规则藏在字段名下**：`all_passed` 看起来像布尔，实际是一整套反伪绿算法（见 §6）；
- **上下文管理器 + 锁**：`repository.py` 用一把 `threading.Lock` 包住所有 SQLite 访问，配合 `check_same_thread=False`——这是"我自己管理并发正确性"而不是靠框架。

---

## 4. 候选补丁写入口：块协议 → 计算行号 → 双门禁 → 原子应用

```python
# app/tools/patching.py（骨架，按调用顺序）
report = run_gates(skeleton_diff, allowed_paths=..., max_files=...)  # 门禁①：编译前形状
diff_text = compile_block_patch(patch_text)  # 锚点匹配 + 行号由平台算
report = run_gates(diff_text, ...)  # 门禁②：编译后产物
apply_result = apply_diff(workspace, diff_text)  # 不是异常，是结构体
```

```python
# app/gitops/patcher.py（顺序）
landing = check_landing(paths)  # symlink → symlink_escape；resolve 逃逸 → path_escape
rc = git(["apply", "--check", "--whitespace=nowarn"], input_bytes=diff_text)  # stdin，不进 argv
snapshot = read_bytes_of_touched_py_files()
rc = git(["apply"], input_bytes=diff_text)
rescan = detect_escape_after_apply()  # TOCTOU 复扫
if rescan:
    git(["apply", "-R"], input_bytes=diff_text)  # 反打回
if not ast_parses(new_content):
    restore(snapshot)  # 语法失败按字节还原
```

**在干什么**：模型的补丁不是 diff，是**块协议**（`*** Begin/End Patch` + 锚点文本）。行号由 `SequenceMatcher.get_grouped_opcodes(3)` 计算（`blockpatch.py`），锚点 0 命中 / 多命中 / 乱序都直接拒。门禁跑两次是为了同时拦住"坏意图"和"坏编译产物"。
**为什么**：模型写 unified diff 最常见的失败就是行号偏移和上下文不符；把行号从契约里拿掉，失败模式从"打不上"变成"锚点不明确"，后者可以喂回模型让它自己修正。
**会被怎么问**："补丁部分应用了怎么办？"→ "`git apply` 全成或全不成，失败返回结构体；真正危险的是 TOCTOU，所以应用后再复扫一次，违规就 `git apply -R` 反向打回，反打失败也如实上报、不自动掩盖。"
`Python 语法点`：**`difflib.SequenceMatcher`**（标准库做行号计算，不用第三方）；**bytes 快照 + `os.replace` 原子写**（`candidate.py` 拒绝空补丁、tmp+replace 落盘）。

---

## 5. 七项门禁：每一条都对应一次真实事故

```python
# app/graph/gates.py（判定要点，非原文）
if not diff_text.strip():
    violation("format", "diff is empty")
elif "diff --git" not in diff_text and not diff_text.startswith(("--- ", "+++ ")):
    violation("format", ...)
for rel in files:
    if is_test_file(rel):
        violation("files", "modifying test file is forbidden")
    if (
        ".." in parts
        or ".git" in parts
        or rel.startswith(("/", "\\"))
        or re.fullmatch(r"[A-Za-z]:", parts[0])
    ):
        violation("paths", "path escapes workspace")
    if not path_allowed(rel, allowed_paths):
        violation("paths", "outside allowed scope")
for rel in parse_new_files(diff_text):  # 影子模块（N-1）
    if any(name in _RESERVED_TOP_LEVEL for name in candidates):
        violation("shadow", ...)
for rel in parse_new_symlinks(diff_text):  # mode 120000 一律拒（P3-6/R3-Q4）
    violation("files", "new symlink is forbidden")
if len(files) > max_files:
    violation("scope", ...)
return GateReport(ok=not violations, violations=violations)
```

**读代码时要抓到的三个"为什么"**：
1. **冒号只按 Windows 盘符形态拒绝**（注释复盘 P2）：早期实现把所有含 `:` 的路径当绝对路径，误杀 `weird:name.py` 这类 POSIX 合法文件名 → 说明门禁会有**误杀成本**，收紧与放宽都是语义变更（AGENTS.md 把它列禁区的原因）。
2. **影子模块门禁**：新增顶层 `pytest/` 或 `json.cpython-314-...so` 会让 `python -m pytest` 的 `sys.path[0]=cwd` 劫持**判定层本身**。这是本项目最精彩的一条安全设计——它防的不是改测试，而是**替换裁判**。
3. **符号链接与目标无关一律拒**：注释理由很硬——"链接目标的合法性是文件内容，静态文本无从审计"。**承认静态分析的边界并据此收紧**，这是可以当亮点讲的判断力。

**实际测试**：`tests/test_attacks.py` 用 `bugs/attacks/` 的 10 个目录逐条验证对应门禁拦截，且断言工作区仍未被污染。

---

## 6. 反伪绿判定：`all_passed` 为什么不是一个 `rc == 0`

```python
# app/adapters/pytest_adapter.py（节选，docstring 摘要 + 关键分支）
if self.exit_code != RC_OK or self.timed_out: return False
if self.failed or self.errors or self.skipped: return False     # 跳过也算不过
if not self.requested_ids: return True
...  # 每条 requested id 解析成 TestIdentity，解析不了即不过
distinct_cases = 去重后的 (file, classname, name) 三元组
for identity in identities:
    matching = [c for c in distinct_cases if case_matches(identity, *c)]
    if len(matching) != 1: return False     # 0 = 被 deselect；≥2 = 身份歧义
    if matching[0] not in passed_statuses: return False
return True
```

**为什么值得逐行讲**：注释直接写了攻击方式——"仅凭 `exit_code==0` 防不了伪：仓库内 conftest 可以把用例 skip 或 deselect，junit 的 rc 依然是 0"。而 `≥2 判不过`修的是 F3 缺陷：只比"文件名 + 方法名"时，`TestB::test_same` 会被当成满足了 `TestA::test_same`。
**顺带**：`build_pytest_cmd` 强制 `-o junit_family=xunit1`，注释解释"xunit2 不写 `file` 属性，classname 随 rootdir 漂移"，因为匹配以 `file` 为主判据。→ **判定依赖输出格式细节**，这是很真实的工程约束。
**会被怎么问**："你怎么确定它不是把测试藏起来而不是修好了？"→ 三层：测试文件禁止修改（门禁 2）、skip/deselect 一律判不过（本段）、点名测试必须唯一命中（本段）。然后**主动补一句**："但这只防手段不防意图，过拟合补丁我防不住——那需要隐藏测试集。"

---

## 7. 终局验收：一个合取式管住所有引擎

```python
# app/graph/acceptance.py（原文）
resolved = (
    validation == VALIDATION_PASSED
    and gate == GATE_PASSED
    and diff_non_empty
    and resource_status == "within_budget"
    and not cancelled
)
```
`validation` 需要 `verify_ran` 且失败集与回归集都绿且**双跑不矛盾**（矛盾 → `inconclusive` → NEEDS_REVIEW）；`gate` 需要 `gate_ran and gate_ok`；账本 `exhausted/unknown` 一票否决；token 用 `max(state, ledger)` 复核；graph 还要候选绑定——工作区 diff 的 sha256 必须等于冻结的 `candidate_hash`。

**为什么最值钱的是模块 docstring**（`acceptance.py`）：它记录了缺陷现场——"graph 的 finish 节点无条件写 FINISHED/resolved，plain 的判定段只看两个布尔；两引擎都不在终局复查'未超预算、非空 diff、取消状态'。修法：两引擎调用**同一个** helper。"→ 面试里这就是"我发现过什么 bug、怎么修"的完美答案：**缺陷是两个实现各自判定，修法是收敛判定而不是补断言**。

---

## 8. 上下文压缩与容量：把"塞不下"和"压不动"分成两件事

```python
# app/context/token_window.py（行为）
compact_messages():  Pass A 把旧工具结果换成"工具名+参数"存根；Pass B 整回合组丢弃（不造孤儿）
protected_region():  头部约束 + 尾部钉住的回合组 = 压不动的那一段
# 调用侧（plain_loop.py）
if estimate_request(protected_region(messages), schemas).total_tokens > capacity_ceiling:
    record(CONTEXT_CAPACITY_EXCEEDED)     # 不再反复压缩
```
**为什么**：注释讲得很直白——"压缩只会把额度耗光再发一发照样装不下的请求"。**软窗口关掉（=0）也要过这一关**，"关闭软压缩"从不等于"宣称端点装得下"。
**顺带的诚实性设计**（M6/ADR-0014）：能力档案必须带来源（`operator_config`/`provider_docs`/`probed`），没来源落保守档，**申报更大一律拒绝发起**，不从模型名推断窗口；端点回报的 `served_model` 与请求名不一致就标漂移。
**会被怎么问**："上下文超了怎么办？"→ 先压缩、压缩不看预算只看保护区、保护区都塞不下就明确失败，并说明"我不会让它在压缩空转里烧钱"。

---

## 9. 执行器：argv、env 白名单、超时杀进程树

```python
# app/executor/local_runner.py（关键行）
env = build_env(os.environ)                    # :32-56 只保留 _ENV_ALLOWLIST
kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP   # :117 POSIX 用 start_new_session
proc = subprocess.Popen(command, cwd=..., env=..., stdin=DEVNULL, stdout=PIPE, stderr=PIPE)  # :125
try: out, err = proc.communicate(timeout=timeout)
except subprocess.TimeoutExpired:              # :137 超时是业务结果，不是异常
    kill_tree(proc)                            # :79-96 taskkill /F /T /PID 或 killpg(SIGKILL)
    ...三段兜底：proc.kill() → 限时 communicate → 关管道   # :141-156
```
`cmd.py` 同理：`["git", "-C", str(cwd), *args]` + `subprocess.run(..., input=input_bytes)`。
**为什么强调**：这是"注入面"的正面答案（无 shell、无字符串拼接、diff 走 stdin）。
**诚实的另一面**：兜底只关管道不 reap，逃逸孙进程可能留存；`whitelist.py` 只匹配 basename（靠"argv[0] 永远由平台设置"这个调用方约束兜住）；**默认后端没有任何资源配额**。
`Python 语法点`：**`subprocess` 的 timeout 语义**、`communicate` 与 `PIPE` 的死锁规避、Windows/POSIX 进程组差异（`CREATE_NEW_PROCESS_GROUP` vs `start_new_session`）——问"跨平台怎么做的"时，这段就是答案。

---

## 10. 数据库与事件流：三张表 + 终态不可复活

```python
# app/storage/db.py
tasks(status, result_json, task_spec_json, task_spec_hash, stage, last_event_at, ...)
trajectory_events(id AUTOINCREMENT, task_id, event_id, ...)   # :104+ 唯一 (task_id,event_id) 部分索引
patches(...); trajectory_events_duplicates(...)               # 归档重复事件
# app/storage/repository.py
:138 set_status_unless_terminal   :150 finalize_task(先到先赢)   :168 cancel_task_row
:285 upsert_event_live(同时推进 stage/last_event_at)
```
**在干什么**：`trajectory_events.id` 自增号就是 **SSE 的续传游标**（客户端带 `last_event_id`，`routes.py`）；`test_runs`/`evaluations` 两张表已退出当前 schema/读写路径（不自动删旧数据库里的历史表）（`:54-60` 留注释），因为判定结果已经全在 `tasks` 与工件里——企划书已同步当前业务表与迁移归档，旧表仅在历史数据库中可能残留。
**会被怎么问**："迟到事件会不会把已完成任务改回运行中？"→ "`set_status_unless_terminal` + `finalize_task` 的'先赢'语义保证终态不可被复活，但进度列 `stage/last_event_at` 允许由 live sink 推进，这是分开的两个字段。"

---

## 11. FakeLLM：回放契约与它的两个特例

```python
# app/llm/fake.py, 71, 110-121
provider = "fake-replay"
steps = json.load(replay_script)  # [{"tool": ..., "args": {...}}, ...]
if PLAN_MARKER in last_user_text and plan_mode != "strict":
    return plan_reply  # 非消费通道：不推进游标
if cursor >= len(steps):
    return finish(success=False)  # 脚本耗尽 = 失败
```
**为什么必须读懂它**：`bugs/<ID>/replay/script.json` 里的 `apply_patch` 步骤**内含完整补丁文本**，而那份文本是 `gen_bugs.py` 从 buggy/fixed 源码 `unified_diff` 出来的——即**标准答案**。
→ 一句话：**FakeLLM 证明的是"平台能不能正确判定一次修复"，不是"模型能不能修复"**。PLAN_MARKER 特例的存在理由是"不重写历史回放夹具"，这是兼容性债的活标本。

---

## 12. Redis / Qdrant 专题（面试八股对撞真实代码）

- **Redis**（`代码事实`）：只有 `locks.py` 的分布式锁；`RedisLock` 用 `SET NX EX` + Lua `if redis.call("get",KEYS[1])==ARGV[1] then del`（token 比对防误删他人锁，`:43-62`）；默认 `redis_url=""` → `InMemoryLock`（其 release 无持有者校验，但 DB 幂等挡住双持有者）。**没有缓存、没有队列、没有 pub/sub**。`风险点`：Redis 故障时现在会直接失败，没有降级路径。
- **Qdrant**（`代码事实`）：**项目里不存在**。`grep -riE "qdrant|embedding|vector|faiss|chroma" app/` 零命中，`pyproject.toml` 无该依赖。如果被问"你的向量库怎么用的"，正确答案是"我没有用向量库，我用 AST 骨架 + rg 预筛 + 逐行了断，理由是……"（见 `02` M6）。**不要因为技能栈里写了 Qdrant 就把它安到这个项目上**——这是最容易一击致命的错配。

---

## 13. 测试体系里最值得讲的三个夹具

```python
# tests/conftest.py
@pytest.fixture(autouse=True, scope="session")
def _basetemp_outside_repo(...):
    assert repo_root not in (root, *root.parents)   # 临时目录父链上不许有本仓库 .git
```
**为什么**：仓库在临时目录父链上时，`git add -A -N` 会"其实不是 git 工作区却静默什么都不加"，夹具缺陷会**悄悄变绿**。这条断言是我见过的最实用的"防止测试自己骗自己"的写法。

```python
# tests/conftest.py  demo_repo：把 tests/fixtures/demo_repo 物化成一次性 git 仓库
# tests/conftest.py pytest_collection_modifyitems：按 tests/slow_tests.txt 自动打 slow 标记
```
`Python 语法点`：**fixture 作用域与 autouse**（session/函数级）、`collect_ignore`（夹具仓库里有故意红的测试）、`pytest_collection_modifyitems` 钩子做**外部登记表驱动的标记**、`pytest_runtest_logreport` 抓"跑了 ≥5s 却没登记"的用例——即**测试套自己也在被治理**。
**实际测试**：`tests/test_slow_registry.py` 校验登记表没有悬空条目、`stress ⊆ slow`。

---

## 14. Python 语法点清单（只列项目真实用到的）

| 语法 | 项目里的位置 | 为什么用 |
|---|---|---|
| `TypedDict` | `graph/state.py`（`total=False`） | LangGraph 状态需可 JSON 序列化；无 reducer → last-write-wins |
| `@dataclass(frozen=True)` | `task_spec.py`、`diagnosis/proposal.py` | 任务契约不可变，可算内容哈希 |
| `@property` | `pytest_adapter.py` `all_passed` | 把复杂判定藏在读起来像字段的名字下 |
| `@lru_cache` | `config.py` `get_settings` | 进程内单例；副作用是测试必须清缓存 |
| `@pytest.fixture(autouse=True)` | `conftest.py` | 每条用例强制隔离设置与临时目录 |
| Pydantic v2 校验器 | `schemas.py` `_exactly_one_source`、`config.py`（pydantic-settings） | 交叉字段校验（bug_id XOR repo_path）；配置全部走 Settings |
| `inspect.signature().bind()` | `registry.py` | 参数校验与业务 TypeError 分离 |
| 生成器 + SSE | `routes.py`、`events.py` | 同步生成器被 `iterate_in_threadpool` 包裹（`风险点`：每流占一线程） |
| `secrets.compare_digest` | `auth.py` | 定长比较防时序侧信道 |
| `threading.Lock` / `ThreadPoolExecutor` | `repository.py`、`service.py`、`nodes.py` | 真实并发来源（不是 async） |
| `async def` | 仅 ASGI 管道 `app.py` | `风险点`：async 未带来真并发 |
| `difflib.SequenceMatcher` | `blockpatch.py` | 由锚点计算 unified diff 行号 |
| `sqlite3` 原生 + WAL | `db.py`（`check_same_thread=False`） | 单文件零运维；并发正确性自己管 |
| 异常分层 | `errors.py` | `TaskError/ExecError/GateError/BudgetError/TaskCancelled/Model*Error`，异常携带已消耗事实 |

---

## 15. 如果只能读 5 段代码（Day 1 收尾自测）

1. `app/graph/acceptance.py` —— 判定是什么。
2. `app/graph/gates.py` —— 护栏是什么。
3. `app/graph/plain_loop.py` —— Agent 循环与终止是什么。
4. `app/tools/registry.py` —— 动作面与错误契约是什么。
5. `app/adapters/pytest_adapter.py` —— "通过"这件事怎么定义。

能用 3 分钟把这 5 段连成一条链（受理 → 循环 → 工具 → 门禁 → 判定），Day 1 目标即达成；连不成，就回到 `00` §2 的箭头图重走一遍。


## 本轮接线校准

TaskSpec 的 settings_for_policy 生成任务配置副本，服务/两引擎及报告使用该配置；
pytest 显式接收冻结后端和超时。graph 候选分支与 diagnose 红/绿基线同样传递该后端。
损坏的 ledger_state.json 保留并标 unknown，拒新额度与 resolved；不因恢复重新发预算。
registry 将签名绑定与 handler 执行放在不同异常范围，内部 TypeError 如实报告为执行失败。
对应回归见 test_frozen_policy、test_preflight、test_budget_reservations、test_tools。

终局共享acceptance直接复核deadline，防止VERIFY已绿但门禁/diff处理期间越时仍resolved；graph传用户等待顺延后的实际deadline。
