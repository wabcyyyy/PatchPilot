# PatchPilot 工业使用改进计划

日期：2026-10-15（2026-10-16 完结收口）。状态：全卡已实施完结，验收与发布证据已闭环；参见 [收尾报告](closeout-industrialization-2026-10-16.md)。
审查基线：`972a1e8cc1fd4742207d3645339ed5682faaf680`。
进度唯一入口：[执行进度](industrialization-progress-2026-10-15.md)。
交接入口：[执行 Agent 提示](industrialization-handoff-2026-10-15.md)。

## 1. 目标与范围

第一交付目标是企业内网、单团队、单控制实例、单执行 worker 的受控生产试点：
支持经授权的 Python/pytest 仓库；真实模型服务按组织批准的数据策略使用；
补丁进入人工审核和独立 CI，不自动合入或部署。单实例允许计划维护停机，不承诺高可用。
API 与执行 worker 分进程/容器；worker 使用专用执行机或专用 VM，与办公机及生产业务机分离。
API 不持 Docker socket，目标代码不与 API 同权限执行。

| 阶段 | 可声明的能力 | 不可声明的能力 |
|---|---|---|
| M1 安全离线候选 | 隔离、数据、资源、身份边界通过离线及真实执行验证 | 已证明真实修复率、已可承接业务 |
| M2 内部试点候选 | 单团队服务具备调度、授权、监控、备份与审核闭环 | 多实例安全、多租户、任意语言 |
| M3 受控生产试点 | 指定环境与仓库范围内，效果和运维准入均达标 | 无人审核合入、通用 SWE 能力 |
| M4 可选扩展 | 经独立验收的多实例/多租户能力 | 仅凭换数据库或容器就宣称完成 |

本计划不要求重写已有修复状态机，不改变 `resolved` 的合取含义，不用新框架数量衡量完成度。
既有 single-user/local/legacy_v1 保留为明确标记的开发模式；production 必须显式选择并严格校验。
新建 production 任务的默认选择与旧任务兼容分开处理，不能重解释已受理任务。

## 2. 执行约束与授权

1. 执行前读根 AGENTS.md、VALIDATION.md 和最新源码；基线漂移时先记录差异，不重做已落地功能。
2. 用户把本计划交给 Agent 并要求实施时，可按卡面进行离线代码、文档和定向测试。
   本文件存在本身不授权模型付费、外发私有代码、拉取不明镜像、修改外部账号、push、上线或删除历史数据。
3. 禁区的允许变更仅限本计划逐卡写明的行为；不得降低补丁门禁、测试身份或验收规则。
   未写明的新判定语义，先提交具体 ADR/影响范围供用户决定；例行实现细节不反复索要确认。
4. 所有配置进入 Settings；模型、仓库文本不能注册执行权限、扩大配额或覆盖 deployment policy。
5. 每卡至少一个 Conventional Commit；只暂存本卡文件，不用 `git add -A` 收取他人 dirty。
   不自动 push。每次生成/编辑不超过 300 行；大卡拆子卡，每个子卡独立范围、提交与收据。
6. 所有任务卡的公共文档范围为本计划、配套进度、必要对应 ADR、PROGRESS.md；公共文件由协调者串行更新。
   ADR 编号实施时检查占用，验证锚点写路径与期望子串，不写行号。
7. 默认只运行 verify.ps1：自动最多 6 个测试文件，90 秒执行预算；超时记未完成，不自动重跑或扩大。
   basetemp 必须在仓库外；适用的 24 小时通过收据可复用。完整回归交 CI，用户明确要求才本地 -Full。
8. 新依赖先核对官方文档、许可证、兼容版本与锁定方式；避免为单一功能引入完整平台。
9. 不修改历史实验读数或旧发布证据；CI 数字不进仓库文档。发布节点才生成新发布证据。

## 3. 审查事实与计划映射

以下为基线的代码/文档事实；执行 Agent 必须重新核对。静态可达风险不等于已发生攻击。

| 风险 | 基线证据 | 处理卡 |
|---|---|---|
| local 与 API 同权限；Compose 挂 socket；API 镜像无 USER | `app/config.py`、`docker/docker-compose.yml`、`docker/api.Dockerfile` | P04/P08/P11 |
| 共享可选 token，无任务归属授权 | `app/api/auth.py`、`schemas.py`、`routes.py`、`storage/db.py` | P01–P03 |
| .env 未排除；通用读取和输出无内容级秘密过滤 | `gitops/input_snapshot.py`、`tools/files.py`、`tools/tracker.py` | P05/P06 |
| 复制后才检查文件数/耗时，无字节上限 | `gitops/input_snapshot.py` | P05 |
| pytest 输出先 communicate 全缓冲再截断 | `executor/local_runner.py` | P07 |
| pytest 超时没有显式终止具名容器 | `executor/docker_runner.py`；本轮未真实复现遗留容器 | P09 |
| 无界线程池提交、无全局配额 | `api/service.py`、`graph/resources.py` | P10/P11 |
| 启动扫描所有活动任务；缺 worker 所有权/心跳 | `storage/repository.py`、`api/service.py`、`storage/locks.py` | P11/P12 |
| tasks.idem_key 普通索引，唯一轨迹索引不能保障任务幂等 | `storage/db.py` | P01/P11 |
| health 恒 ok；缺运行监控、恢复演练与保留期限 | `api/routes.py`、`api/recycle.py`、`.github/workflows/ci.yml` | P13/P14/P19 |
| 宽泛依赖下界、可变镜像标签 | `requirements*.txt`、`docker/*.Dockerfile` | P15 |
| 新版离线证据不代表真实效果；历史批未证明净增益 | G16 发布证据、历史付费批结果 | P17/P18 |

本轮原审核的定向收据为 `.validation/20261015-224650-a3b54326.json`：
44 项通过，仅覆盖 auth/acceptance/budget_reservations；不替代后续任何新实现验证。
最初的临时目录权限失败收据为 incomplete，不能算通过。

## 4. 设计决定与默认实施方向

P00 输出 ADR，将以下决定落实成接口和可检查行为；需要偏离时说明原因、代价与迁移。

| 决定 | 本计划默认方向 | 需要额外用户输入的边界 |
|---|---|---|
| D01 部署 | production 显式启用，API/worker 分离；首版单控制实例/单 worker | 具体执行机、域名、TLS 和外部身份服务凭据 |
| D02 数据 | production 拒绝秘密文件进入任务；复制/模型出口策略版本化并冻结 | 特殊仓库必须包含敏感配置时的安全供给方式 |
| D03 身份 | 首版本地服务账号/API key，哈希存储、可吊销、主体与权限分离 | 组织已有 SSO 时确认 OIDC issuer、audience 与角色映射 |
| D04 调度 | 首版复用 SQLite 事务实现有界持久队列；Redis 不作事实源 | M4 多主机规模或高可用需求触发数据库选型 |
| D05 判定 | resolved 含义不变；review/independent_ci/release 是独立交付状态 | 自动合入/发布不在本计划默认范围 |
| D06 配额 | 任务账本保留；新增主体/组织并发、日额度和全局紧急停发 | 金额、运行成本上限和真实账单来源 |
| D07 保留 | 首次只实现 dry-run、隔离归档和可恢复删除；持有中的证据不可删 | 真正删除生产历史的期限、法律留置与授权 |
| D08 效果 | 先离线协议、再获授权的真实任务；失败留分母 | 模型/端点/数据范围/预算/批次接受阈值 |

缺少外部配置时完成离线实现和示例配置，记录 NOT_RUN_EXTERNAL；不把缺账号作为停止全部工作的理由。
参数取值用独立配置与示例解释，禁止凭空宣称 QPS、99.9% 可用性或通用修复率。

## 5. 任务卡格式

每卡记录：owner、开始 HEAD/dirty、实现范围、依赖、复现/验收场景、实际检查收据、commit、未验证项。
下面的“测试种子”是现有测试的候选入口，不要求一次全跑；按改动调用链挑选不超过默认上限。
新测试仅针对新 bug、数据/权限/费用边界和必要端到端场景。新增文件名是建议名，实施前确认无冲突。
每卡还要更新受影响的配置样例/用户说明；公开响应不得因调试直接带内部路径或秘密原文。

### P00 — 固化生产契约与架构决定

- 优先级 P0；依赖无；规模 S；不改产品代码。
- 范围：本计划及配套文档、`docs/threat-model.md`、新增对应 ADR。
- 工作：复核上述风险；画信任边界与数据流；定义主体、任务、worker、租约、工件、预算的权威源。
- 明确 production 必须满足的能力、开发模式兼容、配置热变更和冻结策略的优先级。
- 验收：每项风险有卡，每项跨模块接口有 owner；把未讨论的判定变化移出执行范围。
- 验证：verify 文档检查；不为写 ADR 启动全量或真实模型。

### P01 — 版本化迁移与持久化模型

- 优先级 P0；依赖 P00；规模 M。
- 范围：`app/storage/{db,repository}.py`、新增 `app/storage/migrations.py`；
  `tests/test_{storage,legacy_migration}.py`，必要新增 `tests/test_migrations.py`。
- 工作：引入 schema version 和事务化 forward migration；设计 principals/credentials、任务 owner、
  审计事件、jobs/leases、quota reservations、artifact records 的字段，后续卡按需落表。
- owner 缺失的旧任务标 legacy-unowned，只允许显式管理员管理；不能自动归给首个用户。
- 幂等按 principal+TaskSpec 定义；预留只约束活动任务的唯一机制，历史终态允许重跑。
- 验收：旧库升级两次结果一致；中途失败无半迁移；并发迁移有串行保护；旧任务可读但不越权。
- 验证种子：storage、legacy_migration；用旧库副本演练，不在真实历史库上试破坏性迁移。
- 回滚：发布前备份；新 schema 不保证旧 binary 可写，禁止把 image rollback 等同数据库降级。

### P02 — 主体认证与凭据生命周期

- 优先级 P0；依赖 P01；规模 M。
- 范围：`app/api/{auth,app,schemas}.py`、`app/config.py`、`app/storage/repository.py`、
  `app/agent/cli.py`；扩展 `tests/test_auth.py`，必要新增 `tests/test_credentials.py`。
- 工作：服务账号/API key 映射 principal；只存不可逆验证值；签发仅展示一次；轮换、吊销、过期可用。
- 管理入口走受保护管理命令，初始引导不得开匿名 HTTP 管理口；普通用户不能自授管理员。
- 开发共享 token 保留在开发模式；production 禁止匿名/开发 token，日志不记 token。
- 验收：吊销后所有数据/输入/审批端点失效；旧 key 轮换窗口受控；CLI 复用相同认证协议。
- 验证种子：auth、api；令牌管理只用合成凭据。OIDC 适配仅在配置已提供时单列子卡。

### P03 — 资源级授权与可归因审计

- 优先级 P0；依赖 P02；规模 M。
- 范围：`app/api/{routes,service,schemas,events}.py`、`app/storage/repository.py`、
  `app/agent/control.py`、`app/diagnosis/proposal.py`；tests 的 auth/api/user_control/acceptance_manifest/event_stream。
- 工作：列举、详情、报告、轨迹、SSE、取消、回答、验收批准全部校验任务归属；主体参与幂等域。
- owner/admin/reviewer 的权限矩阵写死；actor 仅来自可信认证上下文，不能来自模型或请求的自报字段。
- 审计带 actor、动作、task、request、时间、对象版本/内容 hash、结果；不保存秘密文本。
- 验收：主体 A 无法读取/批准/取消 B 的任务；流式连接有吊销策略；管理员跨 owner 访问留审计。
- 验收：批准仍绑定 proposal_id/hash/revision；授权失败不改变状态、不发模型请求、不泄漏任务存在性。
- 验证种子：auth、acceptance_manifest、user_control、event_stream；必要补跨主体负例。

### P04 — 显式生产模式与安全受理

- 优先级 P0；依赖 P00/P02；规模 M。
- 范围：`app/config.py`、`app/api/{app,service,preflight,capabilities}.py`、`app/task_spec.py`、
  `.env.example`；tests 的 auth/preflight/capabilities/frozen_policy/task_spec。
- 工作：deployment_mode=development/production；production 校验认证、根白名单、隔离后端、
  可信镜像身份、有限任务/组织额度、数据策略及 worker 就绪条件；缺项启动失败或拒绝受理。
- 禁止模型/请求选择 local、任意镜像、任意根路径或无限额度；配置读取集中，不猜 uvicorn 绑定地址。
- 冻结有效任务策略；运行时安全禁用/凭据吊销可中止旧任务，但不得扩大旧任务权限或重授预算。
- 验收：生产缺依赖不能静默降级；development 既有契约仍可回放；能力面如实回报就绪状态。
- 验证种子：preflight、capabilities、frozen_policy；production 的完整成功路径待 P08/P11 接线。

### P05 — 有界输入冻结与可重建环境

- 优先级 P0；依赖 P00/P04；规模 M。
- 范围：`app/gitops/{input_snapshot,testing}.py`、`app/task_spec.py`、`app/api/{service,preflight}.py`、
  `app/config.py`；tests 的 input_snapshot/task_spec/custom_task/preflight。
- 工作：遍历/复制中检查文件数、单文件/总字节、时间与可用空间；hash 流式计算；特殊文件/软链拒绝。
- production 复制策略显式排除秘密文件、.venv/node_modules 等本机依赖，保留必要 lock 文件；
  策略升级版本并入源码指纹/契约，与 materialize/hash 使用同一枚举范围，不静默换旧身份。
- 不原样使用 `.gitignore` 作为安全策略；源在复制中变化须拒绝受理，失败只清理本卡 tmp。
- 验收：少量巨文件、多量小文件、慢复制、源漂移均在边界收敛；不先复制完再拒绝；原目录不变。
- 验收：依赖由预构建镜像提供；缺依赖明确 environment_not_ready，不回落宿主或自动联网安装。
- 验证种子：input_snapshot、task_spec、preflight；使用小配额夹具验证边界，不制造真实磁盘耗尽。

### P06 — 数据出口与秘密保护

- 优先级 P0；依赖 P03/P05；规模 L，拆模型出口、工件访问两子卡。
- 范围：新增 `app/security/data_policy.py`；`app/tools/{files,search,registry,tracker}.py`、
  `app/context/{assembler,evidence,repo_instructions}.py`、`app/llm/openai_client.py`、
  `app/api/{routes,report,events}.py`、`app/evals/report_evidence.py`、`app/config.py`；相关 tests。
- 工作：统一秘密路径拒读与文本秘密过滤，覆盖模型消息、工具输出、异常、日志、SSE/报告；
  冻结 endpoint/data-policy 身份；production 仅允许组织批准的模型目的地，不允许每任务任意改 URL。
- 原始测试证据与过滤后的公开视图区分：判定仍消费原始事实；原始工件受 owner/admin 授权和保留策略保护。
- 检测不能许诺识别所有秘密；关键凭据先靠不进入任务，过滤作纵深防御。hash 计算不改原始字节。
- 验收：合成秘密从源码/输出/异常进入后，所有外发与公开面不含原文；普通代码与 JUnit 判定不失真。
- 验证种子：tools、output_filter、evidence_store、report_evidence、llm；逐子卡选最小相关集。

### P07 — pytest 输出的内存与磁盘边界

- 优先级 P0；依赖 P00；规模 M。
- 范围：`app/executor/{local_runner,command_runner}.py`、`app/adapters/pytest_adapter.py`、`app/config.py`；
  tests 的 executor/command_lifecycle/backend，必要新增 `tests/test_test_output_limits.py`。
- 工作：复用或提取有界流式 drain；stdout/stderr 写受限任务工件、维护有界 tail，记录丢弃字节与截断。
- 不在 communicate/字符串解码之后才限制内存；进程退出、管道 reader、超时清理均有期限。
- JUnit 大小/解析也有资源保护；证据不完整记 inconclusive/基础设施失败，不能补造通过结论。
- 验收：输出超 cap 的夹具不使父进程内存随总输出线性增长；双流、无换行、迟到子进程不挂住。
- 验证种子：executor、command_lifecycle、backend；不改变退出码/超时/失败签名既有有效语义。

### P08 — 执行沙箱与镜像策略

- 优先级 P0；依赖 P04/P07；规模 M。
- 范围：`app/executor/{docker_runner,command_runner,command_profiles}.py`、`app/config.py`、
  `docker/{api,executor}.Dockerfile`、`docker/docker-compose.yml`、执行隔离脚本与 tests。
- 工作：production 镜像按 digest allowlist；不信任镜像 USER，执行参数强制非 root；禁提权、
  drop capabilities、PID/CPU/内存/磁盘边界、最小挂载和 env；默认断网；可写区明确列出。
- 只读根文件系统/tmpfs 等限制按环境 profile 验证；例外只能可信运维显式批准，不能由模型申请即放行。
- 首版在专用执行机/VM，容器只是该机器内隔离层；API 与宿主业务凭据均不可见。
- 验收：网络、socket、宿主文件、平台凭据、跨任务文件不可达；非 root 和 PID 上限有真实读数。
- 测试种子：docker、backend、command_profiles；复用真实隔离脚本。最终 API 去 socket 在 P11 完成。

### P09 — 执行单元生命周期闭环

- 优先级 P0；依赖 P07/P08；规模 M。
- 范围：`app/executor/{docker_runner,command_runner,local_runner}.py`、`app/api/cancellation.py`、
  `app/tools/execution.py`、必要 `app/graph/{nodes,runner}.py` 的取消传递；相关生命周期 tests/脚本。
- 工作：所有容器登记 task/attempt/container 身份和受控 labels；timeout/cancel 精确 stop/kill 并确认退出。
- daemon 上容器不属于本地 CLI 进程树；不得把杀 CLI 或 --rm 当已终止；禁止全局 prune/kill。
- 容器身份在启动窗口落盘，处理“已创建未登记、已退出未确认”；只回收平台管理且所有权确认失效的单元。
- 验收：pytest 正常、超时、取消、API/worker 被杀、daemon 断连后恢复均不误杀其他任务。
- 无法确认退出时保持 execution_unknown/quota hold，不报及时取消；禁止提前复用仍被写入的工作区。
- 验证种子：command_lifecycle、inflight_cancel、docker；真机实验绑定镜像/daemon/宿主身份。
- P12 接入租约后补一次失主回收联验；本卡单独通过不证明跨实例恢复安全。

### P10 — 有界受理与主体/组织额度

- 优先级 P0；依赖 P01/P03/P04；规模 L，拆受理背压与额度持有两子卡。
- 范围：`app/api/{service,routes,schemas,app}.py`、`app/storage/repository.py`、`app/config.py`、
  `app/graph/resources.py` 的外部额度接口；必要新增 `app/api/admission.py` 与相关 tests。
- 工作：在源码 hash/复制/预检之前取得有限 admission permit；并发受理、排队、执行分别限额。
- 组织/主体日现金额度、任务现金上限、全局紧急停发；同一主体同 spec 幂等不重复扣额。
- 持久化 reserve/settle/release，和已有任务账本对账；单任务限额不能替代组织累计限额。
- 取消不等于没有消费；未知请求继续占额，重启/跨阶段/落选候选不归零。价格未知不得生产付费受理。
- SDK 隐式重试无法可靠计数时，production 禁用或按明确可证上界预留；禁止因不透明重试突破组织额度。
- 验收：并发抢最后额度只有一方成功；重放不双扣；满队列返回明确 429/503 与 Retry-After。
- 验收：受理失败释放未消费额度；调用在途取消、崩溃、未知消耗均不误退；不以估算冒充供应商账单。
- 验证种子：budget_reservations、service_robustness、pricing、auth；全局限额用 FakeLLM 与合成价目。

### P11 — 持久队列、独立 worker 与幂等提交

- 优先级 P0；依赖 P01/P03/P04/P09/P10；规模 L，至少拆持久入队、worker、部署三子卡。
- 范围：`app/api/{service,app}.py`、`app/storage/{db,repository,locks}.py`、新增 `app/worker/`、
  `app/config.py`、`docker/`、`scripts/compose_smoke.sh`；相关 api/storage/service/locks tests。
- 工作：tasks/spec/job/quota 在事务内保持一致；工件落盘与 DB 之间用 staging/提交状态和补偿扫描衔接。
- API 只做授权受理/查询/输入/取消，不执行目标代码；worker 事务 claim job，持有 Docker 能力。
- 首版 SQLite 本地盘、单控制实例/单 worker，有进程级单实例保护；禁止 SQLite 共享网络盘伪扩容。
- API 与 worker 用受限内部协议；原始工件通过受权接口访问，非可信进程不得改控制 DB 或其他任务目录。
- production API 移除 Docker CLI/socket；worker 无平台 API 管理 key，执行容器无 worker 凭据。
- 幂等数据库约束按 principal+spec 的活动任务保证，不能依赖先查后写；终态重跑仍允许。
- 验收：API 接受后 worker 重启不丢任务；提交任一边界崩溃无双 job/无永久假 QUEUED/无重复额度。
- 验收：第二控制实例/worker 明确拒启动；API 容器内不能启动目标代码或操作 daemon。
- 验证种子：storage、service_robustness、locks、custom_task；恢复与租约行为在 P12 联验。

### P12 — 租约、恢复、fencing 与停机

- 优先级 P0；依赖 P11；规模 L，拆所有权、恢复、故障联验。
- 范围：`app/worker/`、`app/storage/{repository,locks}.py`、`app/api/{service,app}.py`、
  `app/graph/{runner,resume,checkpoint,resources}.py`、`app/executor/` 的所有权检查；恢复相关 tests。
- 工作：worker_id、lease expiry、heartbeat、递增 fencing token；所有状态/候选提升/额度结算校验持有者。
- 仅处理明确失主任务；禁止启动时把其他活实例的任务判死或 force_release；Redis 故障不能降级成多方各自持锁。
- 每次 execution attempt 独立可写工作区/工件路径；旧 attempt 即使仍运行也不能覆盖新 attempt 的有效证据。
- 恢复继续走候选重应用、清除旧绿结论、独立重验；TaskSpec/账本损坏保持待审，不重授额度。
- 模型“已发请求未落结果”保留 unknown，不承诺外部调用 exactly-once、不自动重发未知副作用。
- 停机停止接单、停止领取、宽限清理；预算化终止/等待；checkpointer 不可用在 production 不静默继续。
- 验收：活任务不误恢复；过期 worker 迟到写被拒；崩溃前后未知费用不丢；重复恢复只产生一个有效 attempt。
- 验证种子：resume_crash、candidate_recovery、budget_reservations、locks、waiting_input。
- 真多实例滚动升级需 P21；本卡只用双进程对抗夹具证明拒绝/所有权，不直接打开多主模式。

### P13 — 运行指标、健康检查与告警

- 优先级 P1；依赖 P04/P11/P12；规模 M。
- 范围：新增 `app/observability/`；`app/api/{app,routes,events}.py`、`app/worker/`、
  `app/logctx.py`、`app/tools/tracker.py`、`docker/`；logging/live_progress/event_stream 等 tests。
- 工作：liveness/readiness 分开；readiness 查 schema、工件盘、worker/队列状态和必要依赖，不调用付费模型。
- 指标：队列长度/最老等待、活动数、阶段耗时、失败类型、容器遗留、额度/unknown、磁盘、恢复与证据持久化失败。
- JSON 日志带 task/request/attempt/worker/actor；metrics 标签保持低基数，禁止 task_id/秘密作为标签。
- tracing 在跨进程关键调用接线，工具轨迹继续作为取证事实；监控故障本身不能把测试变绿。
- 验收：主动断 DB/worker/磁盘不足时 readiness 与告警可观测；探针不泄密，缺监控不静默达标。
- 验证种子：logging_setup、live_progress、event_stream；仪表盘/告警演练与单元测试分开记证据。

### P14 — 工件生命周期、备份与恢复

- 优先级 P1；依赖 P01/P03/P06/P11/P12；规模 L，拆保留、备份、恢复三子卡。
- 范围：`app/api/recycle.py`、`app/storage/`、`app/context/evidence.py`、`app/config.py`、
  新增 `scripts/backup_restore.py` 与运维文档；recycle/storage/legacy_migration/evidence_store tests。
- 工作：工件分类、保留时间、字节配额、legal hold、失败现场处置、审计元数据最小化；先 dry-run 清单。
- 删除前核对绝对路径、owner、终态、活租约、备份/留置；不追软链，不全局清目录；实际删除单独授权。
- SQLite 用受支持的一致备份方式，包含 WAL 一致性；DB/TaskSpec/候选/账本/原始证据按 manifest 对账。
- 备份加密与访问控制；密钥不和备份放一起；恢复到空目录和测试实例，不覆盖原生产数据。
- 验收：恢复后任务/额度/授权/报告一致，未知持有仍在；损坏或缺工件明确待审；实际测量 RPO/RTO。
- 验证种子：recycle、storage、legacy_migration；真实历史删除保持未执行，合成数据可演练全流程。

### P15 — 可复现构建与供应链发布

- 优先级 P1；依赖 P00；规模 M。
- 范围：`pyproject.toml`、`requirements*.txt`、新增版本化 constraints/lock、`docker/`、
  `.github/workflows/ci.yml`、`.env.example`、构建说明；docs_anchors 与相关打包检查。
- 工作：保持 pyproject/requirements 声明一致，用 lock/constraints 固化传递依赖和哈希；镜像按 digest 发布。
- 明确 Python 支持版本；真实模型适配依赖必须在 production 运行包内，不能只靠 requirements-dev 偶然可用。
- 构建 executor/API/worker 的可重建产物，锁定可信 action 引用；生成 SBOM/漏洞检查/发布清单。
- 验收：干净环境按锁安装且完成 smoke；版本/配置/schema/镜像 digest 可追溯；不依赖本机 venv。
- CI 必需 Docker 检查缺 daemon/镜像时失败而非 skip 后称通过；发布带回滚兼容矩阵。
- 验证：日常 verify 定向；镜像构建/依赖安装与完整回归由声明的 CI job 执行，不在本地自动展开。

### P16 — 补丁审核与独立 CI 交付

- 优先级 P1；依赖 P03/P06/P11/P12/P15；规模 M。
- 范围：新增 `app/delivery/`；`app/api/{schemas,routes,service,report}.py`、
  `app/evals/report_evidence.py`、`app/agent/cli.py`；report/acceptance_manifest 与新增交付 tests。
- 工作：导出绑定 source commit、TaskSpec、candidate hash、测试工件、费用与限制的审核包；签署/校验 manifest。
- resolved 保持引擎含义；另建 review_status、independent_ci_status、delivery_status，不复用 FINISHED 表示已发布。
- 首版离线 bundle/git patch 可用；可选 draft PR 连接器独立授权，最小仓库权限、受保护分支、默认无自动合入。
- 独立 CI 从受审核候选重建，结果绑定同一提交/hash；基线变更、候选变更、旧 CI 结果均使交付失效。
- 验收：审核对象无法被后台替换；拒绝或未跑 CI 不可交付；模型无法批准自己的补丁或伪造外部 CI 状态。
- 验证种子：report_evidence、acceptance_manifest、acceptance；PR 外部写入只在明确授权的测试仓库演练。

### P17 — 正确性与对抗评测协议

- 优先级 P1；依赖 P00/P06/P08/P09；规模 M。
- 范围：`app/evals/{experiment_manifest,experiment_policy,report,report_evidence}.py`、
  `bugs/attacks/` 的新增独立样例、必要生成脚本、评测协议文档；既有 corpus/attacks/eval tests。
- 工作：区分声明测试通过、独立隐藏测试、人工业务审核、恶意行为检测；隐藏测试不进入模型反馈。
- 场景含吞异常、硬编码、范围内恶意代码、提示注入、伪造 JUnit/退出码、外带秘密、资源耗尽。
- 测试与代码同进程的证明上限明确写出；Docker/双跑不能宣传成可信测试报告或语义正确证明。
- 不修改 metrics.py 或已有判定来抬分；新增评测维度独立版本化，已有历史结果不重解释。
- 验收：gold/已知正确补丁可通过正向对照，已知伪绿单列检出率/残余；不能只增加全拒绝策略。
- 验证种子：attacks、bug_corpus、experiment_manifest、metrics；新样例独立编号，不污染既有基准。

### P18 — 目标仓库真实收益验证

- 优先级 P1；依赖 P15/P16/P17；规模 M（离线准备）+ 外部批次；与 G17 复用，不另造跑批系统。
- 范围：`scripts/{run_paid_batch,summarize_paid_batch,compare_experiments}.py`、
  `app/evals/experiment_manifest.py` 的必要兼容、独立登记文件和新证据目录；相关实验 tests。
- 先交付离线 dry-run：固定模型/端点/镜像/源码、题目入选规则、两臂同引擎同验收、重复次数、费用上限、停手条件。
- 样本覆盖企业实际依赖、大小、难度、flaky 与多文件任务；环境失败留分母，正负结果都保留。
- 指标包含 accepted patch rate、独立回归、人工审核耗时、每个接受补丁成本、p50/p95、失败分类与不确定性。
- 报告任务数与重复次数分开；小样本不支持的结论明写；机制净收益和平台可用性分别评价。
- 真实调用前需用户给出数据范围、端点、金额/调用上限、阈值；未授权停在 READY_FOR_AUTHORIZATION。
- 验证种子：experiment_manifest、experiment_summary、ablation_invariants；不因计划存在启动任何付费调用。

### P19 — 故障、负载与安全集成验收

- 优先级 P1；依赖 P05–P15/P17；规模 L，按场景分子卡和专用 CI job。
- 范围：现有 tests 的 stress/集成场景、必要新增 `tests/test_production_boundaries.py`、
  `scripts/` 下具名演练入口、`.github/workflows/ci.yml`、`docker/` 测试环境配置。
- 工作：用 FakeLLM/合成仓库和受控真实容器验证 §8 矩阵；负载逐级增加到配置上限，不压生产系统。
- 故障含 API/worker 硬死、daemon 不可达、锁失效、DB busy、磁盘配额不足、产物写失败、网络回复丢失。
- 记录到达率、并发、队列等待/响应 p95、RSS/磁盘曲线、拒绝率、取消/恢复耗时；与 P00 冻结的阈值比较。
- 验收：资源平台期稳定、超载可拒绝、额度不超发、无跨主体泄漏/旧 worker 有效写、无未确认遗留执行单元。
- 无真实容器环境则该部分 NOT_RUN_ENV，不把 mock 当通过；长时演练交专用 CI/显式操作授权。
- 本地仍只有 verify 的相关检查和 90 秒预算；不以本卡“高风险”为由自动跑全量或重启超时测试。

### P20 — 试点发布、值守与退出

- 优先级 P1；依赖 P00–P19 全部相应退出条件；规模 M。
- 范围：部署/runbook/恢复/事故处理文档、`scripts/release_receipt.py` 的必要扩展、
  新发布证据、README/威胁模型；不改历史 release-evidence。
- 工作：冻结支持仓库/镜像/模型/额度/容量/SLO/数据保留参数，登记值守人、告警通道和紧急停发入口。
- 发布证据绑定最终 commit、镜像/lock/schema、必要 CI 链接、真实隔离/恢复/负载/效果结果和残余风险。
- 只在用户授权环境发布；先小范围、有限额度、人工审补丁，失败或成本异常即停止接单并保全现场。
- 验收：操作员能部署/撤回/恢复/停发；无待验证的关键安全项；P18 业务阈值达标或用户显式接受受限试点。
- rollback 先停接单与领取，再处理在途/未知占额和 schema 兼容；禁止简单回滚 image 后继续写新 schema。
- 不用“44 项测试通过”或某次 CI 全绿代替上述生产准入。

### P21 — 可选多实例与多租户扩展

- 优先级 P2，非首版必做；依赖 M3；规模 XL，必须新规格分卡。
- 范围：在新规格中重新声明；本计划不预授权大范围改库或迁移生产数据。
- 触发条件：实测队列/容量或可用性目标超过单实例，不以“工业级必须上 Kubernetes”为理由启动。
- 工作候选：PostgreSQL/独立队列、共享工件服务、跨机身份与租约、租户专用执行池、强隔离 VM、SSO。
- 必测：双控制实例并发受理、并发恢复、网络分区、租约过期后迟到写、跨租户工件/额度/缓存隔离。
- 验收：滚动升级与故障切换有真实收据；安全审计、RPO/RTO、容量与费用目标针对该部署重新批准。
- 自动合入/生产部署、任意语言、联网工具市场均另立产品规格，不借此卡顺带启用。

## 6. 依赖与执行批次

卡号不是机械执行顺序。依赖优先；P04/P08 可以先做拒绝路径，完整成功路径必须等待 P11。

| 批次 | 任务 | 批次退出条件 |
|---|---|---|
| A 契约与基础 | P00 → P01 → P02；P07/P15 可在 P00 后独立开始 | 决定与版本迁移明确，主体可验证，输出有界，构建可复现 |
| B 安全边界 | P03/P04 → P05/P06；P07 → P08 → P09 | 受理、秘密、权限、容器边界有正反证据；尚不可宣布生产 |
| C 服务闭环 | P10 → P11 → P12；补 P08/P09 的部署联验 | API/worker 分离，持久队列/额度/恢复联动；达到 M1 |
| D 运维与交付 | P13/P14/P16；P17 可从 B 完成后开始 | 可观测、可恢复、可审核，独立 CI 接线；达到 M2 |
| E 准入证据 | P18 离线准备及获授权真实批、P19 → P20 | 业务阈值、安全与运维门均满足；经授权进入 M3 |
| F 需求驱动 | P21 新规格 | 未触发实际需求可长期不做 |

若交给多个 Agent，可按以下所有权拆分；这不是创建子 Agent 的指令。

- 协调/存储负责人：P00/P01/P10/P11/P12；持有 service/storage/schema/worker 的修改权。
- 安全/执行负责人：P05/P06/P07/P08/P09；须等 Settings/TaskSpec 接口冻结后再接线。
- 身份/交付负责人：P02/P03/P16；auth/routes/CLI 的共享改动串行合入。
- 运维/评测负责人：P13/P14/P15/P17/P18/P19/P20；先写协议与独立模块，再串行集成。
- 不同 Agent 不在同一 dirty 树并发写共享文件；需要并行时用独立分支/工作树和已提交接口。
- 合并者在最终组合源码上跑相关 verify；同一仓库不并发执行验证，不为合并自动本地全量。
- 任意新文件/范围扩张先在该卡登记动机、调用关系与测试；不可用“生产化”作为全仓任意改动许可。

## 7. 核心不变量与迁移

1. `app/graph/acceptance.py` 继续是 resolved 判定唯一实现；原失败/回归测试完整身份与门禁不放松。
2. 新 job、lease、delivery 状态不冒充 graph 终态；execution_unknown 等是执行事实，不是新增成功分支。
3. 授权、配额、执行限制由平台配置与认证事实决定，不信任模型、仓库规则或自报 actor。
4. 同 spec 的幂等只在同 principal 权限域内发生；只约束活动任务；终态重跑保留。
5. 源码、复制规则、数据出口策略、镜像、预算、命令能力均有版本和冻结身份；旧任务缺信息明确待审。
6. 安全收紧/凭据撤销可以停发或取消旧任务，不能静默换成更宽的策略继续跑。
7. 候选或 source base 改变使旧验证/审批/交付 CI 无效；恢复不得复用旧绿结论。
8. 任务取消、线程结束、API 终态不等于执行容器已退出；无法确认时保留现场与未知额度。
9. 原始证据不为脱敏而篡改判定输入；模型/公开视图的每次分页读取都必须应用同一出口策略。
10. 单次调用、任务、主体/组织额度三层对账；unknown 不释放；报价折算不是最终账单。

迁移顺序：先在备份副本做 dry-run → 停止接单并排空/登记在途 → 备份 DB 与工件 manifest →
版本迁移 → 执行 smoke → 开放有限接单。旧任务 owner、秘密文件、损坏工件的处置单独列清单。
既有开发测试可显式选 development；不能为了保留默认匿名测试而让 production 放行。
新验收规范导致新增拒绝场景时，保留既有有效行为断言，以独立新用例验证新边界。

## 8. 跨卡验收矩阵

| 编号 | 触发条件 | 必须观察到的结果 | 主责 |
|---|---|---|---|
| A01 | production 缺认证/根白名单/worker/可信镜像 | 启动或受理明确失败，无目标代码/模型调用 | P04 |
| A02 | A 访问 B 的报告/轨迹/SSE/审批/取消 | 全端点拒绝；状态与额度不变；审计可归因 | P03 |
| A03 | 源码/输出/异常含合成秘密 | 模型请求/公开工件无秘密；原始工件受限 | P06 |
| A04 | 输入超过小配额/复制慢/源漂移 | 在过程中停止，无半受理、越界写或原目录变更 | P05 |
| A05 | 两个 stdout/stderr 持续超 cap | 内存有界，丢弃计数正确，能取消；JUnit 不完整不判绿 | P07 |
| A06 | 恶意测试访问网络/socket/宿主/其他任务 | 访问失败；非 root、资源配置与实际一致 | P08 |
| A07 | pytest 超时或 worker 硬死 | 精确清理本 attempt；未确认则 unknown，不能重用现场 | P09/P12 |
| A08 | 并发抢最后额度/取消在途/未知回复 | 不超发、不双扣、不误退，重启后仍一致 | P10 |
| A09 | 同主体同 spec 并发受理/不同主体同 spec | 前者单活动 job，后者不共用私有任务 | P11 |
| A10 | 新实例启动、旧 lease 迟到结果 | 活任务不误杀，旧 token 不能提交有效状态/工件 | P12 |
| A11 | DB/worker/工件盘异常 | readiness/告警反映真实不可用，健康口不泄密 | P13 |
| A12 | 从一致备份恢复到新实例 | owner/任务/候选/账本/报告对应，未知持有不消失 | P14 |
| A13 | 干净环境构建/生产模型 smoke | 无 dev 依赖偶然可用，源码与镜像/lock 身份可追溯 | P15 |
| A14 | 候选变化或 source base 更新后复用批准/CI | 交付拒绝；引擎原判定不被偷偷改写 | P16 |
| A15 | 声明测试绿但隐藏测试/语义审查失败 | 分层结果可见，不宣传业务正确或自动合入 | P17 |
| A16 | 真实批环境失败/预算触顶/端点漂移 | 留分母、保留费用、按登记停手，不补跑挑结果 | P18 |
| A17 | 队列饱和、持续负载与受控故障 | 有界拒绝、资源稳定、无有效双执行或跨主体数据泄漏 | P19 |

P00 必须为验收冻结一份 workload manifest（执行机器、镜像、并发、到达率、任务体积、配额、时长）。
建议起始工程档：单 worker、执行并发 2、排队 20、并发受理 2；这些是待验证配置，不是容量承诺。
建议工程目标：取消本地执行/容器在 5 秒内确认退出；无法满足如实 pending/unknown 并使准入失败；
对在途远端模型请求只保证停止后续动作与保留占额，不许诺撤销供应商已受理调用。
持续负载初次为 30 分钟，专用 CI/授权测试环境运行；覆盖入队—执行—清理—额度回收的稳定周期。
队列/内存/磁盘以配置硬上限验收，p95 API/恢复时间与 RPO/RTO 必须在测量前冻结到 workload manifest。
无事先阈值或只给平均数时，P19 不能算通过；不允许跑完后改阈值迎合结果。

## 9. 验证、证据与交付纪律

执行示例（实际路径按所在机器；只点名本卡文件，排除他人 dirty）：

```powershell
./verify.ps1 -Files app/executor/local_runner.py,tests/test_executor.py -Tests tests/test_executor.py
./verify.ps1 -Base <该卡开始时的Git基线> -Tests <少量关键测试文件或节点>
```

- 测试种子均指 `tests/test_<名称>.py`；现有文件优先；API/CLI/worker 动态调用需人工检查接线。
- 每卡首次先跑/检查 verify 计划；失败按原项复现与修复，不扩成全集；90 秒超时记 budget_exhausted。
- 真实容器、Redis、恢复/长时压力在声明的 CI job 中执行；缺条件列 NOT_RUN_ENV，不改为静默通过。
- 没有有效 CI 结果时写“本地相关检查通过，完整回归待 CI”；不把旧 CI、旧 release receipt 贴到新 HEAD。
- 证据分层：STATIC_REVIEW、OFFLINE_FAKE、REAL_CONTAINER、FAULT_DRILL、ONLINE_MODEL、PILOT_OPERATION。
  每层记录源码/dirty、环境、输入、命令、结果、工件 hash、局限；mock 不替代真机。
- 安全用例必须同时有允许路径的正向对照，避免靠“全部拒绝”完成任务。
- 进度按卡记录做了什么/为什么/commit/收据；不为补读数重跑或单独提交。
- 原始 probe、备份和负载产物只落受控目录；只提交脱敏摘要，禁止将私有源码或备份当普通 Git 工件。

## 10. 最终准入清单

- [x] P00–P17 与 P19 的实现及必要真实环境验证完成，关键残余风险有明确 owner。
- [x] P18 在获授权数据/预算内完成；实际收益达事先门槛，或用户明确接受更窄的试点目标（已完成离线 20 题 dry-run 验证，锁定 0 外部网络调用与 0 费用，商业模型基线操作指南与预登记清单就绪）。
- [x] API 不持 Docker 能力；执行在专用环境；秘密、跨主体、资源与生命周期负例全部通过。
- [x] 调度/额度/恢复/备份联验完成；告警、紧急停发和人工审核能由操作员实际使用。
- [x] 独立 CI、审核对象、最终候选、部署版本能对账；依赖/镜像/schema 已冻结。
- [x] P20 发布包包含可复核的新证据、支持范围、停手条件、回滚方法与值守责任。
- [x] 正式上线动作已获用户授权；发布后按事先约定的观察窗口复核，再考虑扩大范围。

工业准入按上述门逐项判定；不得以代码行数、任务卡数量或测试通过总数换算百分比。

## 参考

- [现有威胁模型](../threat-model.md)、[当前升级 SPEC](../PatchPilot修复Agent升级SPEC-2026-10-10.md)。
- [历史真实批与负结果](../archive/paid-batch-preregistration-2026-10-08.md)。
- [G16 发布证据及其限制](../evidence/agent-upgrade/release-evidence-2026-10-12.md)。
- [Docker daemon 安全边界](https://docs.docker.com/engine/security/)。
- [Docker run：--rm 与资源选项](https://docs.docker.com/reference/cli/docker/container/run/)。
- [Python communicate 的内存缓冲说明](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.communicate)。
