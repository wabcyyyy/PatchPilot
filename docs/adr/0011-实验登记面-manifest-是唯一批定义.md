# ADR 0011:实验登记面 —— manifest 是唯一的批定义,控制身份与观测结果分开

日期:2026-10-11 | 状态:已采纳 | 素材:SPEC §7 G04、评审 `agent-readiness-review-2026-10-10.md` R5、runs/paid-batch-ledger-2026-10-09.jsonl

## 背景

对照实验的可信度取决于"除登记变量外全部相同"这条纪律,而旧实现有三个方向相反的缺陷
(全部在 `scripts/compare_experiments.py` / `app/evals/provenance.py` / `scripts/run_paid_batch.py`
里实测确认,不是推测):

1. **结果被当控制变量比较**。`verdict/status/tokens_used/rounds/duration_ms` 与配置项混在
   同一张扁平表里逐字段判等:两臂结果不同 ⇒ 报"未登记差异"退出,结果相同 ⇒ 被当作
   "配置相同"的证据。前者把唯一合法的测量对象判成缺陷,后者是反向推理(结果一致推不出
   条件一致);
2. **配置漂移看不见,而且被制度化地看不见**。`llm_max_tokens`、`refine_head_lines/tail_lines`、
   `max_read_lines`、`llm_thinking`、`llm_max_retries` 都登记在 `EXEMPT_KEYS`(理由写的是
   "只影响喂给模型的文本长度,不改判定与复现口径"),比较器因此根本没在读它们 ——
   窗口/工具上限/thinking 变了,两臂照样"通过比较";
3. **重复被折掉,判据被隐式继承**。`latest_per_bug` 同题只留最新一次,n=3 的方差被压成
   "一次成功";paid driver 的题单、额度、停手条件写死在 `--phase q0` 的常量里,
   新批不显式换判据就静默沿用旧批(本仓库已经为此作废过一次付费实验)。

## 候选

1. **只修比较器字段表**:能解掉 ①,但 ②③ 仍在,登记面还是散在代码常量里;
2. **给每个新实验写一份独立脚本**:判据不再继承,但每批一份脚本 = 每批一套口径,
   历史不可比,也没人保证脚本真的比了控制身份;
3. **一份可 hash 的 manifest + 控制身份/观测结果分离 + 驱动器只认 manifest**(采纳)。

## 结论

- `app/evals/experiment_manifest.py`:题单、臂与**该臂唯一允许的登记变量**、重复数、
  额度、停手条件、授权范围、产物目录集中在一份 JSON 里,`manifest_hash` 参与身份
  (`created_at` 不参与,同一计划重复导出同 hash);在线批缺 `authorization.granted_by/scope`
  或缺 `max_cost_micro_usd` 直接拒绝 —— SPEC §7 G17 说本文档不授权任何真实调用,
  那么"没登记授权"就不能起批。
- 比较器分两栏:`CONTROL_SECTIONS`(git commit、脏树指纹、配置快照、端点/价目/容器/
  协议身份、任务与源码与测试清单身份)必须逐字段相同;`OBSERVED_FIELDS`(verdict/tokens/
  rounds/duration/费用/门禁/错误)允许不同,而且**只有** manifest 登记的变量能在控制栏里
  不同。跨 `provenance_schema_version` 的批次互比直接拒绝:旧批没有这些节,把缺席读成
  "相同"就是假公平。
- 配对键是 `(task_identity, arm, repetition)`,**每次运行都保留**;重复 task_id、缺对、
  次数与 manifest 不符各自单独诊断。`latest_per_bug` 保留给历史汇总口径(不改旧报告解读),
  但比较器不再经过它。
- provenance 新增去敏控制身份:`model_identity` 显式写 `alias_proves_served_version=false`
  与 `served_model=null`(模型名相同不能证明服务端版本没漂);`endpoint_identity` 只留
  规范化 host+path 与整条 URL 的短 hash,userinfo/query/密钥不落盘;`protocol_identity`
  带工具 schema 与提示词指纹、策略 hash;`container_identity` 只查本地 Docker 元数据,
  查不到写 `tag_only/unavailable`,local 后端写 `not_applicable` —— 绝不把宿主执行读成
  "同一个容器环境"。
- `llm_max_tokens / llm_max_retries / llm_thinking / max_read_lines / max_search_results /
  max_output_chars / refine_head_lines / refine_tail_lines / docker_image` 从 EXEMPT 移入
  SNAPSHOT。理由:它们改变模型可见内容与单次请求的形状,不是机器相关噪声。
  `price_overrides` 与 `llm_base_url` 留在 EXEMPT(值是本地路径/可能内嵌凭据),
  但它们的**身份**改由 `pricing_identity` / `endpoint_identity` 以 hash 登记。
- `run_paid_batch.py` 只认 `--manifest`;`--dry-run` 展开矩阵与额度包,零子进程、零网络、
  不建锁、不写台账。旧的 Q0 常量保留在文件里作只读历史记录,用它起真实运行一律拒绝。

## 后果(含反方)

- +:批的定义第一次有了一个可 hash、可归档、可反查的对象;"这批按什么判停"从代码常量
  变成登记项;重复之间的方差第一次进摘要;
- −:每批多写一份 manifest,起批的摩擦变大。这是刻意的 —— 本仓库最贵的两次错误
  (作废付费实验、隐式继承 Q0 判据)都源自起批太快;
- −(反方,必须一起读):**比较器只能证明"控制身份相同",不能证明结果归因于登记变量。**
  同题 n=1 仍不算机制证据(既有裁决),而 `registered_upgrades` 是否真把升级影响包住,
  没有在线证据;协议升级前后跨批比较在离线里只能拒绝,不能替代真实两臂批;
- −:`repetition` 目前由配对结果推导(第几次运行按 task_id 排序编号),运行产物里没有
  rep 字段;要让 manifest 的 rep 与运行一一对上,需要 TaskSpec/RuntimePolicy 的接线点,
  属 G07/G16 的范围。台账条目里记的是 `(bug, arm, rep)` 与原 task_id,不冲突;
- −:`container_identity` 的 digest 只在 Docker 可用时取得;本机 daemon 状态会决定
  这一节写 `checked` 还是 `tag_only`,跨机比较时它是**差异项**而不是噪声,比较器照实报。

## 验证锚点

- `app/evals/experiment_manifest.py:22` — `MANIFEST_SCHEMA_VERSION = 1`
- `app/evals/experiment_manifest.py:122` — `def control_identity(self, arm: Arm) -> dict[str, Any]:`
- `app/evals/provenance.py:281` — `RECLASSIFIED_TO_SNAPSHOT: tuple[str, ...] = (`
- `app/evals/provenance.py:395` — `alias_proves_served_version`
- `scripts/compare_experiments.py:33` — `ALLOWED_DIFFERENCES: frozenset[str]`
- `scripts/compare_experiments.py:150` — `def compare_batches(`
- `app/evals/metrics.py:161` — `def pair_runs(`
- `scripts/run_paid_batch.py:166` — `"--dry-run"`

用例面(非行锚):`tests/test_experiment_manifest.py`(manifest 校验、去敏、dry-run 零请求)、
`tests/test_experiment_summary.py`(T07 接受结果差异、T08 拒绝配置漂移、重复保留、
缺对与重复 ID 诊断、跨结构版本拒绝)。
