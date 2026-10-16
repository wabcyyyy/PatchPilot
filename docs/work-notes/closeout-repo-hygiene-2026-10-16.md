# 仓库整理批次收尾(2026-10-16)

本批次把本地仓库对齐远端收口状态后,做了一轮依赖入口、过程文档与目录结构的整理。
全部为文档/构建面改动,不改产品语义;门禁、验收契约与测试期望零变化。

## 背景

本地 master 曾停在 812a747(B3 G09/G10 平行实现),与远端分叉 2/71。经核实远端已有
同一批卡的另一实现(1b6fdec/d3cf0c3/99ab509)并完成 P00–P21 产业化收尾(ef44a06),
确认无损后 `git reset --hard` 对齐(被弃提交 reflog 可寻约 90 天)。

## 本批次提交(自 ef44a06 起)

| commit | 内容 |
|---|---|
| 179c486 | 删 TODO.md 与 VALIDATION.md(规则本就在 validation-rules.json/verify.ps1),改写 AGENTS/README/archive 索引/代码注释引用 |
| 747af73 | 依赖入口并入 uv.lock:pyproject dev 迁 [dependency-groups],CI 改 setup-uv + `uv sync --frozen` + `uv run`,api.Dockerfile 改 uv 安装;锚点测试的依赖对账从 requirements 双入口改为锁文件对账(静态名单 + `uv lock --check`) |
| e7bcfa7 | 清理上轮 head 截断漏掉的 VALIDATION/TODO 引用(scripts/README、企划书、ai_validate.py 死集合条目、measure_basetemp_contention 判据指针) |
| 6666399 | work-notes 三件 2026-09 审查材料归档 docs/archive/;eval-report 两份移 docs/evidence/eval/(report.py 默认输出与引用同步,interview-guide 重建站点);补 MIT LICENSE |
| 15d96ec | PROGRESS.md(192KB 总日志)归档 docs/archive/:两线均已收口,AGENTS 记账节改"各线进度写 work-notes";锚点测试对归档台账按文件名豁免(历史链接按当时仓库根写) |

## 明确不动的(留痕,防止后续误清)

- `scripts/` 一律不重排:README §6 明文禁止(路径被 ADR/PROGRESS/evidence 按文件名引用);
- `verify.ps1` + `validation-rules.json` 留根:验证入口面同层是刻意设计;
- `constraints.lock` 保留:P20 发布证据引用的当时 pin 快照,uv.lock 接管今后锁定;
- bugs/、demo/、adr/、postmortems/、interview-guide/(含生成站点)为活内容或有意提交;
- 本地忽略数据目录(data/eval_suite/.swe_cache/runs)按用户选择保留。

## 验证与边界

每步提交前跑 `ruff check` + `ruff format --check` + 定向测试
(test_docs_anchors / test_report / test_ai_validation,共 49 用例)全绿;
完整回归交 CI(本批次改了 CI 流程本身,push 后以该 run 为准)。
归档遵循 docs/README.md 约定:只搬移与改引用,不改历史数字与结论;
PROGRESS.md 正文一字未动,仅位置变化。
