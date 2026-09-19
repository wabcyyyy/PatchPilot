# PatchPilot 夜间自动开发 SPEC(GOAL 模式)

> 日期:2026-09-18 | 基线:master `c35d5da`,**129 个测试全绿** | 必做 5 张卡 + 选做 2 张
> 本文档是夜间无人值守执行的**唯一任务来源**。开工前必须先读仓库根 `AGENTS.md`;
> SPEC 与 AGENTS.md 冲突时以 AGENTS.md 为准。文中所有"设计决策"均为已定死结论,
> 执行代理**不得**重新讨论、变更或"优化"——有异议写进晨会报告,不要改实现方向。

## 0. 执行方式

1. 开工先确认基线:`.venv/Scripts/python.exe -m ruff check .` 通过且
   `.venv/Scripts/python.exe -m pytest -q` 恰好 129 passed。基线不绿 → 停止一切,只写报告。
2. 卡序:N1 → N2a → N2b → N3 → N4a → N4b →(选做 N5)→(选做 N6)。
3. 每张卡:读卡 → 把"改哪些文件、加什么测试"两三行追加到 `runs/night-log-2026-09-18.md`
   → 实现 → 跑该卡验收 → commit → 下一张。
4. 时间盒(分钟):N1=60,N2a=60,N2b=90,N3=90,N4a=90,N4b=60,N5=60,N6=90。
   超时未达验收 → 停手,在 night-log 写 3 行复盘(现象/已试方案/卡点)→ 下一张。
   同一方案重试不得超过 2 次。
5. 允许部分完成的卡,commit message 标注 `[partial]` 并列明剩余工作。

## 1. 硬性红线(违反任何一条 → 立即停止该卡并记录)

1. **零真实花费**:不设 `PATCHPILOT_LLM_ENABLED=true`,不构造任何真实 LLM 请求,
   不安装新依赖(openai 包已在 dev 依赖,允许 import 用于离线构造)。
2. **禁区语义冻结**:不改 `tests/` 既有期望;不放宽任何门禁拦截;
   `app/graph/`、`app/gitops/`、`app/executor/local_runner.py`、`app/tools/` 边界校验、
   `app/adapters/pytest_adapter.py` 签名规则、`app/evals/metrics.py` 判定逻辑
   只允许本 SPEC 明示的、向后兼容的扩展(加参数/加字段/加 except 分支),不做其他语义变更。
3. **绿线纪律**:每张卡 commit 前必须 `ruff check . && ruff format --check . && pytest -q` 全绿;
   禁止 skip/删除/改写既有测试来"过绿";新增测试失败不许提交。
4. **提交纪律**:Conventional Commits;每卡 ≥1 commit;禁 `git push`(无远端);
   禁改历史提交;禁改 `ci.yml`、`AGENTS.md` 与本 SPEC;单次生成 ≤300 行;
   只改卡内"范围"声明的文件。
5. **候选题隔离**(N5):一切候选题目只进 `bugs_candidates/`(仓库根,新建),
   严禁写入 `bugs/`——正式集与评测、CI 都不能被污染。

## 2. 环境事实(照做,不要重新发现)

- Windows + Git Bash;解释器:`.venv/Scripts/python.exe`(uv venv,无 pip 模块);
- pytest basetemp 已在 `pyproject.toml` 配置(`--basetemp=.pytest-tmp`),如遇临时目录 ACL 报错,
  显式加 `--basetemp=.pytest-tmp/night`;
- 格式检查:`.venv/Scripts/python.exe -m ruff format --check .`;
- 无 `.env`、无真实 API key——这是预期状态,不要创建 `.env`;
- Docker Desktop 守护进程可能未启动:N6 开工前先 `docker info`,失败即跳过该卡并记录;
- SQLite 库在运行目录按需生成;compose 映射 8001(勿占用该端口起服务)。

---

## N1 API Bearer 鉴权(必做,60min)

- **目标**:为 API 增加可选 Bearer Token 鉴权,作为花费防线的最后一环。
- **设计决策(定死)**:
  - `Settings` 新增 `api_token: str = ""`;空 = 不鉴权(现有测试与本机使用完全不受影响);
  - 新文件 `app/api/auth.py`:FastAPI dependency,读 `Authorization: Bearer <token>`,
    比对用 `secrets.compare_digest`;缺失/不匹配 → 401,
    统一错误结构 `{"code": "unauthorized", "message": ..., "task_id": null}`;
  - `GET /api/health` 豁免(单独注册或 dependency 内豁免,选实现最简者);
  - 其余 `/api/*` 全部保护,包括 cancel 与 report。
- **范围**:`app/config.py`、`app/api/auth.py`(新)、`app/api/app.py` 或 `app/api/routes.py`、
  `tests/test_auth.py`(新)。
- **测试**:`token 未配置 → 匿名全通过(回归)`;`配置后无 header 401`、`错 token 401`、
  `对 token 201/200`、`health 豁免`、`report/trajectory/cancel 均被保护`。
  测试内用 `Settings(api_token="t")` + `get_settings.cache_clear()` 控制,结束后恢复缓存。
- **验收**:`pytest -q tests/test_auth.py tests/test_api.py` 全绿;全量绿后 commit。
- **commit**:`feat(api): 可选 Bearer Token 鉴权(api_token,空=关闭)`

## N2a Token 明细贯通 prompt/completion(必做,60min)

- **目标**:当前全链路只有 `total_tokens`,成本核算需要输入/输出分开计价。
- **设计决策(定死)**:
  - `AssistantTurn` 加字段 `prompt_tokens: int = 0`、`completion_tokens: int = 0`(向后兼容);
  - `openai_client.complete` 解析 `usage.prompt_tokens/completion_tokens`(缺失回退 0,
    total 回退估算的现状不变);
  - `FakeLLM`:`prompt_tokens=0`、`completion_tokens=usage_tokens`(估算值);
  - `LoopOutcome` 与 `TaskResult` 各加 `tokens_prompt`/`tokens_completion` 累计字段;
    `plain_loop` 累计、`nodes.localize/propose` 汇入 state、两个 runner 写进 report.json;
  - `tokens_used` 语义不变(total),新字段是增量信息。
- **范围**:`app/llm/base.py`、`app/llm/fake.py`、`app/llm/openai_client.py`、
  `app/graph/plain_loop.py`、`app/graph/nodes.py`、`app/evals/driver.py`、
  `app/graph/runner.py`、相关 tests。
- **测试**:openai_client 解析三元组;plain_loop/驱动器跑完 fake 任务后
  report.json 含 `tokens_prompt`/`tokens_completion` 且 ≥0;现有断言不受影响。
- **验收**:全量绿;`runs/` 一次 fake 回放的 report.json 含新字段。
- **commit**:`feat(llm): prompt/completion token 明细贯通到 report`

## N2b 成本核算与入库(必做,90min,依赖 N2a)

- **目标**:每个任务折算美元成本,落库 + 进报告。
- **设计决策(定死)**:
  - 新文件 `app/evals/pricing.py`:内置 `PRICES: dict[str, tuple[float, float]]`
    (键=模型名,值=(输入, 输出) USD/1M tokens),至少收录
    `deepseek-chat`、`deepseek-reasoner`、`gpt-4o-mini`、`gpt-4o`(价格写"约值"即可,报告注明);
    `estimate_cost(model, prompt_tokens, completion_tokens) -> float | None`:
    精确匹配,未命中返回 None(不猜测);provider 以 fake 开头 → 恒 None;
  - `Settings.price_overrides: str = ""`(可选 JSON 文件路径,同构 PRICES,优先级高于内置);
  - 模型名来源:report/任务的 `model_provider` 是 "openai",真实模型名用
    `Settings.llm_model`(service._execute 处可得,贯通到 TaskResult 新字段 `model_name`);
  - **migration**:`db.connect` 建表后检查 `PRAGMA table_info(evaluations)`,
    缺列则 `ALTER TABLE` 追加 `cost_usd REAL`、`tokens_prompt INTEGER`、`tokens_completion INTEGER`;
  - `repository.upsert_evaluation`、`service._persist_artifacts`、`report.json`、
    `app/api/report.py` Markdown 各加一行成本输出(USD,4 位小数;None 显示 n/a)。
- **范围**:`app/evals/pricing.py`(新)、`app/config.py`、`app/storage/db.py`、
  `app/storage/repository.py`、`app/api/service.py`、`app/api/report.py`、
  `app/evals/driver.py`、`app/graph/runner.py`、相关 tests。
- **测试**:价目命中/未命中/覆盖文件/fake 恒 None;**旧库 migration**
  (手工建旧 schema 库 → connect → 新列存在且旧数据完好);报告 Markdown 含成本行。
- **非目标**:不做汇率、不回填历史任务、不追求计费精确(报告注明"约值")。
- **commit**:`feat(evals): 任务成本核算(价目表+SQLite migration+报告)`

## N3 cancel 协作式中断(必做,90min)

- **目标**:cancel 从"只改状态"升级为"真正停止执行"(现执行线程会烧完所有轮次)。
- **设计决策(定死)**:
  - 不强杀线程,协作式:`threading.Event` 按 task_id 注册;
  - 新文件 `app/api/cancellation.py`:`CancelRegistry`
    (`register(task_id) -> Event`、`request_cancel(task_id) -> bool`、`unregister(task_id)`);
  - 新异常 `TaskCancelled(PatchPilotError)`(`app/errors.py`,code="cancelled");
  - `plain_loop` 加参数 `cancel_event: threading.Event | None = None`,
    每 turn 开头(model.complete 之前)检查 `is_set()` → 抛 `TaskCancelled`;
  - `driver.run_task` / `graph.runner.run_task_graph` 加同名可选参数,向下传递;
    graph 侧 `TaskNodes` 加可选字段并传给两处 `run_plain_loop`;
    `TaskCancelled` 在 driver/runner 捕获 → `result.status="CANCELLED"` 正常落盘(保留现场);
  - `service.cancel_task`:置 DB 状态后调用 `request_cancel`;
    `service._execute` 创建任务时 register,finally unregister;
    `_execute` 捕获 `TaskCancelled` → 不覆盖 CANCELLED 状态(现有保护已满足);
  - 语义注明:中断在下个 turn 边界生效,正在跑的一次 pytest/LLM 调用会先完成。
- **范围**:`app/errors.py`、`app/api/cancellation.py`(新)、`app/api/service.py`、
  `app/graph/plain_loop.py`、`app/evals/driver.py`、`app/graph/runner.py`、
  `app/graph/nodes.py`、相关 tests。
- **测试**:Event 预先 set + 永不结束的 stub 模型 → 抛 `TaskCancelled`;
  API 集成:创建任务(fake 长脚本)→ 立即 cancel → `wait_terminal` 到 CANCELLED
  且不再变化;已终态 cancel 409(现有测试回归)。
- **commit**:`feat(api): cancel 协作式中断(turn 边界生效)`

## N4a 任意仓库任务:数据通路(必做,90min)

- **目标**:启用 `TaskCreateIn` 里预留的 `repo_path + issue` 形态,接入用户自有仓库。
- **设计决策(定死)**:
  - `TaskCreateIn` 加可选字段:`repo_path: str|None`、`issue_text: str|None`、
    `failed_tests: list[str]|None`、`regression_tests: list[str]|None`、
    `allowed_paths: list[str]|None`;
  - 校验:`bug_id` 与 `repo_path` **恰好一个**(用 pydantic model_validator);
    `repo_path` 必须 resolve 后存在且为目录,否则 404;
    用 `repo_path` 时 `issue_text`/`failed_tests`/`regression_tests` 必填,缺 → 422;
  - `allowed_paths=None` 语义:不设白名单;**禁改测试文件由门禁
    `forbid_test_files=True` 无条件兜底(已确认与 allowed_paths 无关),不得改动该规则**;
  - service:按 payload 构造 `BugTask(id=f"CUSTOM-{uuid8}", root=repo_path,
    repo_dir=repo_path, replay_script_path=None, ...)`,落库沿用现有列;
  - **模型约束**:`model="fake"` 仅当 payload 同时提供 `replay_script: list[step]`(可选新字段,
    与 bugs/ 回放脚本同构)时可用;`model="openai"` 走现有总开关前置校验;
    自定义任务 + fake + 无脚本 → 404,提示二选一;
  - 幂等键沿用 `bug_id|engine|model`(CUSTOM id 含 uuid,天然不冲突)。
- **范围**:`app/api/schemas.py`、`app/api/service.py`、`app/evals/bugset.py`
  (仅允许加"内存构造"辅助,不得改 `load_bug` 行为)、相关 tests。
- **测试**:校验矩阵(双给/都不给/路径不存在/缺必填);fake+脚本 与 openai+禁用 两类拒绝路径。
- **commit**:`feat(api): 任意仓库任务接入(repo_path+issue)` 

## N4b 任意仓库任务:e2e 与攻击面(必做,60min,依赖 N4a)

- **目标**:自定义任务全链路可离线验证,且安全语义与正式题完全一致。
- **做法(定死)**:测试内用 `tests/fixtures/demo_repo` 物化到 tmp_path 作为 `repo_path`,
  `replay_script` 给正确修复脚本 → 断言 FINISHED/resolved;
  攻击用例:`replay_script` 试图 `apply_patch` 修改 `tests/` → PATCH_REJECTED,
  试图路径穿越(`../../`)→ 被拒;轨迹与 report.json 三件套齐全。
- **范围**:`tests/test_custom_task.py`(新,或并入 test_api.py)。
- **验收**:`pytest -q tests/test_custom_task.py` 全绿 + 全量绿。
- **commit**:`test(api): 自定义仓库任务 e2e 与门禁攻击用例`

## N5 候选 Bug 题目扩充(选做,60min)

- **目标**:为正式集(20 题)储备人工审题候选,不进任何自动流程。
- **做法(定死)**:新目录 `bugs_candidates/CAND-001..`;每题结构与 `bugs/README.md` 规范一致
  (manifest.yaml + issue.md + repo/ + replay/);
  缺陷类型覆盖现有 20 题未覆盖的形态(如:字典默认值共享、可变默认参数、
  时区比较、字符串格式化注入、生成器一次性消耗等,自行设计但避免与现有题重复);
  新增 `scripts/validate_candidate.py <dir>`:物化 repo → failed_tests 恰好全失败、
  regression 全过 → manifest 必填字段齐全 → replay 两个脚本 JSON 可解析。
- **验收**:≥8 道候选,每道 `python scripts/validate_candidate.py` exit 0;
  `pytest -q` 全量不受影响;候选不出现在 `list_bug_ids`(它在 `bugs/`,天然隔离)。
- **commit**:`feat(bugs): 8 道候选题与验证脚本(待人工审题)`

## N6 Docker 执行后端骨架(选做,90min,探测制)

- **开工条件**:`docker info` 成功;失败 → 跳过并在报告记录。
- **目标**:为"测试在容器里跑"铺骨架,**不动任何禁区调用点**(集成接线留给白天人工)。
- **设计决策(定死)**:
  - `Settings.execution_backend: str = "local"`(仅接受 local|docker,非法值启动即报错);
  - 新文件 `app/executor/backend.py`:`run_tests_by_backend(cmd, cwd, timeout) -> TestRunResult`,
    按 settings 分发 `local_runner.run_tests` / `docker_runner`(已有);
  - 本卡只做:分发器 + docker 命令拼装单测(monkeypatch subprocess,不起真容器)+
    设计笔记 `docs/docker-backend-notes.md`(接线点分析:哪些调用点在禁区、白天怎么接)。
- **范围**:`app/config.py`、`app/executor/backend.py`(新)、
  `tests/test_backend.py`(新)、`docs/docker-backend-notes.md`(新)。
- **commit**:`feat(executor): 执行后端分发骨架(docker 集成留待人工验证)`

---

## 3. 晨会报告(收工必写)

输出 `docs/night-report-2026-09-18.md`,内容:
1. 每卡状态:完成 / `[partial]`(剩余工作)/ 跳过(原因);
2. commit 清单(`git log --oneline c35d5da..HEAD`);
3. 测试数变化:129 → N(必须只增不减;若减少,属红线违规,如实标注);
4. 新增/修改文件清单;**建议人工 review 的 diff**(按改动量排序,前 3);
5. 遗留问题与卡点;候选题清单(N5 若做)。

## 4. 全局完成定义

必做卡(N1、N2a、N2b、N3、N4a、N4b)全部完成或带复盘的显式降级,
全量 `ruff + pytest` 绿,夜-log 与晨会报告落盘,working tree 干净(全部已提交)。
选做卡(N5/N6)不计入完成定义——时间不够直接不做。
