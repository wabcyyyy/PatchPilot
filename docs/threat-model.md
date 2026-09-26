# PatchPilot 威胁模型(T12.1,如实记录)

> 目标读者:在部署/评审 PatchPilot 前想清楚"它能被怎么滥用"的工程师。
> 原则:明确说清**防了什么、没防什么**;不在文档里许诺不存在的边界。

## 1. 定位与前提

PatchPilot 是**单机个人工具**:给定本地 Git 仓库与 Bug 描述,Agent 生成补丁并跑测试。
它**不是**多租户 SaaS,默认部署形态是本机进程或回环绑定的 compose(见 §5)。

## 2. 资产与信任边界

| 资产 | 说明 |
|---|---|
| 宿主文件系统 | `repo_path` 指向的目录及其可达文件、环境变量(含 LLM key) |
| 宿主计算资源 | 测试执行的 CPU/内存/磁盘(预算门禁只管 token/轮数/时长,不限带宽) |
| 钱包 | LLM API 调用费用 |

信任边界:`API 客户端 → API 服务 → Agent(LLM 输出不可信)→ 七个受控工具 + 七项门禁
→ 执行器(local=进程级 / docker=容器级)`。LLM 生成的补丁与工具参数**一律视为不可信输入**。
API 响应(TaskOut)刻意收敛:不含 idem_key/repo_path/内部主键,但**保留 run_dir
服务端路径**(单机定位产物需要)——多租户部署前必须去掉(R2 整改注记)。

## 3. 风险清单与缓解

### R1 任意 repo_path = 任意目录读 + 任意代码执行

`POST /api/tasks` 的 `repo_path` 可指向宿主任意目录:Agent 可读该目录下文件
(read_file/search_code/list_files),并**在该目录执行 pytest**——而 pytest 会 import
被测包,等价于执行该目录及其 import 链上的代码。
缓解:compose 仅绑回环;可选 `PATCHPILOT_API_TOKEN` 鉴权;`PATCHPILOT_ALLOWED_REPO_ROOTS`
根白名单(T12.2);容器部署时 API 容器只见 `/data` 与 `bugs/`
(例外:`/var/run/docker.sock`,为 M11 隔离闭环所必需,代价与前置条件见 R6)。

### R2 默认无鉴权

`api_token` 为空即关闭鉴权——这是**有意的本地默认**,只与回环绑定组合才成立。
非回环部署必须开启 token(生成:`python -c "import secrets; print(secrets.token_urlsafe(32))"`)。

### R3 不可信模型代码的执行(设计使然,非缺陷)

平台的本质就是"跑模型产的补丁 + 跑目标仓库的测试"。防线依次是:

1. 静态门禁:禁改测试文件 / 路径越界 / allowed_paths 范围 / 文件数上限 / 影子模块
   / diff 格式 / `git apply --check`,攻击样例 9 个拦截(`bugs/attacks/`,回归于 `tests/test_attacks.py`);
2. 命令边界:Agent 只能跑 manifest 预定义测试集,无 shell,参数列表 + 白名单;
3. 执行隔离:`local` 后端=宿主子进程(信任级别≈开发者自己跑测试),`docker` 后端=
   容器级(`--network=none`、内存/CPU 限额、非 root uid 1000、`--rm` 用后即焚);
4. 判定不信任模型声明:resolved 由平台重跑双测试集 + 门禁自动判定。

### R4 提示注入

`issue_text` 与仓库内容(注释、README、测试名)都是模型输入面,可诱导 Agent 写出
恶意"修复"。残余风险:注入可让 Agent 在 allowed_paths **内**写恶意代码——静态门禁
不判语义,最终由"测试集判定 + 执行隔离"兜底;local 后端下等价于"执行了目标仓库
开发者自己也会执行的代码"。

### R5 密钥与成本

`PATCHPILOT_LLM_API_KEY` 经环境变量/.env 注入,不入库不入日志;.env 已 gitignore。
真实调用有三重闸:`PATCHPILOT_LLM_ENABLED` 总开关(默认关)、单请求
max_tokens/超时/重试、单任务 token 预算门禁(BUDGET_EXCEEDED)。

### R6 docker.sock 挂载 = 宿主守护进程等价权限

`execution_backend=docker` 的 compose 部署需挂载 `/var/run/docker.sock`,
能操作守护进程≈能操作宿主。这是把执行隔离从"进程"升级为"容器"的代价,
仅在信任 API 调用方的前提下使用(见 docs/docker-backend-notes.md)。

### R3a 软链残余风险(2026-09-26 补记,审计 R3-Q4 实证;ATTACK-009 声明范围外)

ATTACK-009 的自述范围是「越界路径」(落点 `../escape.txt`,被静态路径门禁拦下)。
其之外存在一条**合法路径软链**残余链,两端实测均已钉死:

- **攻击面**:补丁以 `new file mode 120000` 在合法落点新建软链(落点原不存在),
  指向工作区外模块。逐环节实测:静态门禁不拦(`gates.py` 的 `_NEW_FILE_RE`
  只识别 new file 段、从不解析 mode 值)、落点校验不拦(`patcher._target_violation`
  只查「落点已是软链」与「resolve 越界」,新路径两查皆过)、`git apply --check`
  不拦、verify 阶段被消费——Linux 容器内「先删既有 pkg/util.py → 同路径新建
  120000 软链指向工作区外 evil_mod.py」两步变体(每步独立过门禁)后,
  verify 的 pytest `from pkg.util import helper` **import 了工作区外模块,
  测试通过**;
- **不放大面**:Windows 宿主(local 默认,`core.symlinks=false`)实测 git apply
  落普通文件或报错 rc=128,链条在 git 层断掉;软链落点若被 read_file 触碰,
  `relpath_within` 校验会拦;后续触碰同路径的补丁、测试/控制面命名规则(影子
  门禁)也会拦;
- **裁决**:属 ATTACK-009/E1 已声明范围之外的残余风险,如实记录而非宣称已防。
  「new file mode 120000 一律拒」属门禁语义变更(`gates.py` 边界,AGENTS 红线),
  须另行评审后再实施(见 docs/人工触发清单-2026-09-26.md)。

## 4. 明确不防(边界外)

- 多租户隔离、配额、审计(单机工具定位);
- 恶意/越权的宿主本机用户(回环端口谁都能连,见 R2 的 token 缓解);
- 物理访问、内存窃听、侧信道;
- Agent 补丁的"语义恶意"——平台判定的是"测试通过 + 范围合法",不是"代码善良";
- **被测代码与判定器同进程**(audit-2026-09-20 N-1 残余):verify 阶段运行的是
  Agent 改过的代码,它理论上可伪造 junit/退出码。影子门禁(新增文件不得与
  pytest/stdlib 同名)封掉了最直接的伪造通道,但"恶意仓库作者在基线里预置
  伪造逻辑"(基线仓库本身不可信)不在防线上——自定义任务请确保基线仓库可信,
  或用 docker 后端 + 可信基线;
- **合法路径软链新建**(R3a,2026-09-26 实证补记):new file mode 120000 的
  合法落点软链在静态门禁/落点校验/git apply 三关都不设防,Linux 容器内
  verify 阶段可消费工作区外模块——缓解与裁决见 §3 R3a;门禁层修复(120000
  一律拒)属语义变更,评审前不做。

## 5. 部署形态与适用边界

| 形态 | 执行隔离 | 适用 |
|---|---|---|
| 本机进程(local 后端) | 进程级 | 完全自用,目标仓库本来就会在本机跑测试 |
| 本机进程(docker 后端) | 容器级 | 想隔离被测代码执行的个人开发机 |
| compose(回环 + 可选 token) | 容器级(挂 sock) | 单人服务的长期部署;**不要**暴露到非回环网络 |

## 6. 已知边界与残余风险的其他记录

见 `docs/design.md` §8(单进程架构、协作式取消粒度等)、
`docs/archive/2026-09-18-改进计划M10-M13.md`(已完成并归档)与
`docs/audit-2026-09-19.md`(全量自检的**未整改**风险清单,按 P0–P3 分级)。
