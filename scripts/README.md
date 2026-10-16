# `scripts/` 职责分类

本目录**不是产品入口**：项目没有 `[build-system]`/`[project.scripts]`，不可 pip 安装，
所以每个脚本都以 `python scripts/<name>.py …` 手工执行。CI 里只有**一条**脚本命令——
冒烟步骤的 `uv run python scripts/gen_bugs.py BUG-001 BUG-002 BUG-003`（重新物化题目再回放），
其余脚本都不进 CI；被测试 import 或点名的除外（见下表「被谁钉住」列）。

`ruff.toml` 已并入根 `pyproject.toml` 的 `[tool.ruff]`（2026-10-13 配置去重），
`extend-exclude` 仍把 `bugs/`（生成物）与 `runs/`（产物）排除在 lint 之外。

## 1. 日常开发闸（改动前先看根目录 `validation-rules.json`）

| 脚本 | 作用 | 被测试钉住 |
|---|---|---|
| `ai_validate.py` | 计划先行的本地验证编排（stdlib only），读根 `validation-rules.json` | `tests/test_ai_validation.py` |
| `ai_validate_run.py` | 串行执行 + 绑定输入的回执 + 失败传播 | 同上 |
| `ai_validate_quick.py` | 默认少量测试选择、回执复用、进程预算 | 同上 |
| `ai_validate_graph.py` | 保守的静态 import 选路（动态消费方需人工复核） | 同上 |

根目录的 `verify.ps1` + `validation-rules.json` 是这套闸的**入口面**，
两者同层放置是刻意的：`verify.ps1` 用 `$PSScriptRoot` 定位规则文件，规则文件里也自列自己的路径。
把它们搬进 `scripts/` 要同步改 4 处代码路径与 2 处文档，收益不值这个风险，故留在根。

## 2. 题目集与数据（产物落 `bugs/`）

| 脚本 | 作用 | 备注 |
|---|---|---|
| `gen_bugs.py` | 把题目 spec 物化为 `bugs/BUG-xxx/`（repo + issue + manifest + 回放脚本） | 题目集唯一生成源；**CI 冒烟步骤会执行它** |
| `gen_candidates.py` | Hard 候选题生成（多文件/协同题），产物入 `bugs/candidates/` | 现有 `BUG-C108` |
| `validate_candidate.py` | 候选题入场检查（人工审题前的自动门槛） | `tests/test_validate_candidate.py`（3 条 slow） |
| `import_swebench.py` | SWE-bench 元数据 → 本地任务 | **允许联网**、人工触发、不进 CI |

## 3. 批次对照与发布证据

| 脚本 | 作用 | 备注 |
|---|---|---|
| `compare_batches.py` | 零成本回放批逐题对照（判定/成本回归闸） | 夜间升级循环用 |
| `compare_experiments.py` | S10a 实验比较器：登记差异之外一切不同即报错 | 两臂消融的公平性闸 |
| `run_paid_batch.py` | 付费批驱动器 | **只认显式 manifest**，带零网络 dry-run |
| `summarize_paid_batch.py` | 付费批台账汇总 | 由 `run_paid_batch.py` 派生 |
| `release_receipt.py` | 发布收据生成器（`release-receipt.json` + 证据索引输入） | 输出落 `docs/evidence/` |

## 4. 执行后端与容器验证

| 脚本 | 作用 | 备注 |
|---|---|---|
| `docker_isolation_check.py` | 隔离边界实验：联网、宿主文件访问、可见性、正常执行 | 结论见 `docs/archive/docker-isolation-notes.md` |
| `verify_command_docker.py` | G09 受控命令在真实容器里的隔离与生命周期 | 离线、零拉取 |
| `compose_smoke.sh` | compose 冒烟 | 被 `docker/docker-compose.yml` 与 ADR-0002 引用 |

## 5. 演示与手工 smoke

| 脚本 | 作用 | 备注 |
|---|---|---|
| `demo_gitops.py` | gitops 全流程手工演示 diff → apply → rollback | `app/gitops/snapshot.py` 注释指向它 |
| `build_interview_site.py` | 生成 `docs/interview-guide/site/index.html` | 站点是生成物；同步由 `tests/test_docs_anchors.py::test_interview_site_html_matches_its_markdown` 对账 |

## 6. 一次性实验脚本：**禁止搬动**

这些脚本跑完就退役了，但它们的**路径**被 ADR、`PROGRESS.md` 与
`docs/evidence/` 里的原始输出按文件名引用（输出头部常写「判据见 …」「本脚本只出数字」）。
把它们挪进子目录或删掉，等于让历史证据指向不存在的出处——比留在原地更糟。

| 脚本 | 当时的卡 | 被谁引用 |
|---|---|---|
| `context_replay.py` | A1 上下文重建与 token 估算标定 | `docs/adr/0004`、`docs/adr/0012` |
| `measure_context_counterfactual.py` | A2 工作记忆反事实 | `docs/evidence/2026-10-08-context-counterfactual.txt` |
| `measure_propose_counterfactual.py` | A3 PROPOSE 段反事实 | `docs/evidence/2026-10-08-propose-counterfactual.txt` |
| `measure_basetemp_contention.py` | M11.5.1 并发 pytest 抢临时根 | `docs/evidence/2026-10-08-basetemp-contention-{pre,post}fix.txt` |
| `measure_retrieval_domain.py` | 检索遍历域覆盖度量 | `docs/work-notes/interview-evidence-2026-10-07.md` |
| `probe_g05_bounded_view.py` | G05 有界视图行为收据 | `docs/adr/0012` |

## 7. 曾有零引用、已删除的两个（2026-10-13）

`convert_replays.py`（卡2 的 unified diff → 块协议机械转换）、
`make_failure_demos.py`（失败/拦截演示轨迹生成器）。
删除前的复核口径：`git grep` 全仓除各自文件内部的用法字符串外**0 处引用**，不被任何测试、
CI 或 `app/` 引用，转换目标也已完成（产物在 `bugs/` 里）。历史仍在仓库里可读：

```bash
git show 0343e84:scripts/convert_replays.py   # 或 make_failure_demos.py
```
