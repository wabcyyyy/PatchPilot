"""全局配置:pydantic-settings,环境变量前缀 PATCHPILOT_,支持 .env。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PATCHPILOT_", env_file=".env", extra="ignore")

    # 目录
    runs_root: Path = Path("runs")
    db_path: Path = Path("patchpilot.sqlite3")

    # 模型(OpenAI 兼容端点;留空则仅回放模式可用)
    llm_enabled: bool = False  # 真实 LLM 调用总开关,默认关闭以防误配 key 即产生花费
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 4096  # 单次 completion 输出上限;0 = 不限制
    llm_timeout_seconds: float = 120  # 单次请求超时(SDK 默认 600s,过长会拖垮任务)
    llm_max_retries: int = 1
    # G07:平台显式重试的**额外**物理尝试次数(每次都单独计量,见 TransportRetryPolicy)。
    # 默认 0 = 与引入该字段之前逐字相同(一次就交差,重试完全由 SDK 上界决定)。非 0 只允许
    # 在 llm_max_retries=0 时使用:两层重试同时开就看不见真实发出次数,用量无法对账,
    # 所以构造处直接拒绝而不是"取一个最大值"。
    llm_platform_retries: int = 0
    llm_thinking: str = ""  # 思考模式:空 = 服务端默认;disabled = 关闭;low/high/max = 强度

    @field_validator("llm_thinking")
    @classmethod
    def _check_llm_thinking(cls, value: str) -> str:
        if value not in {"", "disabled", "low", "high", "max"}:
            raise ValueError("llm_thinking must be '', 'disabled', 'low', 'high' or 'max'")
        return value

    # 基础设施(可选)
    redis_url: str = ""
    docker_image: str = "python:3.11-slim"
    api_token: str = ""  # API Bearer Token;空 = 不鉴权(本地与现有测试不受影响)
    allowed_repo_roots: str = ""  # repo_path 根白名单(逗号分隔绝对路径);空 = 不限制(个人本地模式)
    price_overrides: str = ""  # 可选 JSON 文件路径(同构 PRICES,优先级高于内置价目)
    execution_backend: str = "local"  # 测试执行后端:local | docker
    # P3-7 如实化(2026-09-26):原注释「容器集成留待人工验证」与事实矛盾——
    # docker 后端已接线并有 09-18 端到端产物(runs/docker-e2e/、docker-backend-notes.md
    # 记「已接线并真机验证」,隔离边界见 docker-isolation-notes.md 五实验);
    # 默认 local 的理由见 ADR-0002(单机工具;CI/离线评测不依赖守护进程状态)。
    # P3-9:进程级日志级别(此前 25 键无 log_level,判定为"漏"——业务日志零装配)
    log_level: str = "WARNING"
    # P3-12:终态产物回收——取证集(report/diff/trajectory/junit)永久保留,
    # FINISHED 任务的可弃集(workspace/checkpoints)回收;失败/取消现场不回收
    recycle_finished_workspace: bool = True
    recycle_grace_seconds: int = 3600  # 启动扫描只动"终于 grace 前"的行,刚结束的留人看
    # S06 受理冻结的体量与耗时保护(本机单用户定位的如实上界,不虚构超大仓库能力):
    # 超出即受理拒绝(422),提示方修正输入;属运维边界而非复现口径,进 EXEMPT
    intake_timeout_seconds: int = 120
    intake_max_files: int = 20000

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"log_level must be a stdlib level name, got {value!r}")
        return normalized

    @field_validator("execution_backend")
    @classmethod
    def _check_execution_backend(cls, value: str) -> str:
        if value not in {"local", "docker"}:
            raise ValueError(f"execution_backend must be 'local' or 'docker', got {value!r}")
        return value

    # 预算与限制(企划书第 9 节资源门禁的默认值)
    # P1-4/R2 整改:default_max_rounds/max_read_lines/max_search_results 已接线
    # (load_bug、build_custom_bug 与 ToolContext 构造);
    # round_timeout_seconds 曾是零读者死键已删——现行实现为任务级 task_timeout_seconds
    # 在 turn 边界复查(plain_loop),不做单轮独立计时
    token_budget: int = 200_000  # 单任务累计 token 预算;0 = 不限制
    # 额度门禁的**待发请求**读数换算系数(plain_loop 的 turn 边界门禁专用)。
    # 缺陷出处:门禁比的是"provider 真值累计 + 本地估算的单次请求",两个单位;
    # 25 份真实 run 标定出 `len//4` 低估 1.47 倍(逐实例 1.26~1.90,见 M15 与
    # docs/evidence/2026-10-08-context-corpus-calibration.txt),所以这条混单位门禁
    # 系统性地**晚判死**——超支发生在闸门读数还没到顶的那一次请求里。
    # 默认 1.0 = 与引入该字段之前逐字同行为(旧语料的 V1 锚点吃的就是这个读数,不动);
    # 取实测 ρ 才是"统一成 provider 真值口径",但那是**跨模型/跨仓库的一个数**,
    # 要不要当默认值由真实两臂批决定(C 卡),不在这里替它拍板。
    token_estimate_factor: float = 1.0

    @field_validator("token_estimate_factor")
    @classmethod
    def _check_token_estimate_factor(cls, value: float) -> float:
        if not 0.0 < value <= 5.0:
            raise ValueError(f"token_estimate_factor must be in (0, 5], got {value}")
        return value

    # ---------- G03:预算模式与其他维度(默认值 = 既有行为逐字不变) ----------
    # 四类资源互不替代(§5):单请求容量 / 累计计算量 / 资金 / 运行限制。
    # 默认 tokens 模式 = legacy_v1 的那道 token 闸;切 cost/hybrid 必须显式冻结价格与金额,
    # 缺价格时**受理拒绝**(app/task_spec.py 的 validate_budget_policy),不自动联网查价、
    # 也不静默变成无限预算。
    budget_mode: str = "tokens"  # tokens | cost | hybrid
    cost_budget_micro_usd: int = 0  # 现金上限,最小货币单位(1 USD = 1_000_000);0=不设
    max_model_calls: int = 0  # 任务级模型调用数上限;0=不限制
    # 端点**声明**的单请求硬容量(来源须由运维显式给出,不由模型名推断);
    # 0 = 未声明:不做容量检查,也不宣称支持过大窗口。
    model_context_tokens: int = 0
    capacity_margin_tokens: int = 1024  # 容量余量:协议开销与估算误差
    # ---------- G07:能力声明的来源与形状(见 app/llm/profiles.py) ----------
    # 这个数字是"谁说的"。空 = unverified(无来源),此时容量只能落在保守档 16k,
    # 申报更大一律拒绝 —— SPEC §5 禁止把未验证的端点写成"支持 128k/1M"。
    # 合法值:operator_config | provider_docs | probed | unverified | 空。
    model_capacity_source: str = ""
    # 能力清单 `name=yes|no` 逗号串(tools / reasoning / cached_usage)。没写的项一律
    # 记为"未声明"(档案里进 assumed 落盘),不从模型名猜。
    model_capabilities: str = ""
    # 端点声明的 completion 输出上限(与"我们请求的 llm_max_tokens"是两个概念:前者是
    # 能力边界,后者是本次请求的参数;0 = 未声明)。
    model_declared_output_ceiling: int = 0

    @field_validator("llm_platform_retries")
    @classmethod
    def _check_llm_platform_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"llm_platform_retries must be >= 0, got {value}")
        return value

    @field_validator("model_declared_output_ceiling")
    @classmethod
    def _check_declared_output_ceiling(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"model_declared_output_ceiling must be >= 0, got {value}")
        return value

    @field_validator("budget_mode")
    @classmethod
    def _check_budget_mode(cls, value: str) -> str:
        if value not in {"tokens", "cost", "hybrid"}:
            raise ValueError(f"budget_mode must be 'tokens', 'cost' or 'hybrid', got {value!r}")
        return value

    @field_validator("cost_budget_micro_usd", "max_model_calls", "model_context_tokens")
    @classmethod
    def _check_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"budget limits must be >= 0 (0 = 不限制), got {value}")
        return value

    default_max_rounds: int = 5
    task_timeout_seconds: int = 900
    test_timeout_seconds: int = 120
    max_patch_files: int = 5
    max_read_lines: int = 400
    max_search_results: int = 50
    max_output_chars: int = 20_000
    # E4:任务执行线程池并发上限(容量模型见 design.md §8:单进程、local 后端
    # 无 CPU/内存配额、SQLite 单连接写串行——默认 2 是实测基线而非高并发声明)
    task_max_workers: int = 2
    # E3:verify 双跑一致性复核——第一遍双测试集全绿(即将判 resolved)时同命令
    # 重跑一遍比对。价值边界如实声明(P3-5/R3-Q7):防的是**非自适应偶发伪绿**
    # (收集集漂移/flaky/偶发伪造);对基线预置伪造的自适应对手无实质检出力——
    # 同一伪造逻辑在同进程对两次运行同样生效,两次产出完全一致、比对全过
    # (「攻击成本翻倍」无建模支撑,原宣称作废)。定位是**结构性冒烟复核**,
    # 不是防线。关闭只降开销不降拦截,排障时可置 false。
    verify_double_run: bool = True
    # 模型可见层输出折叠的工具消息保留头尾行数(原始 junit/stderr 证据不受影响)
    refine_head_lines: int = 40
    refine_tail_lines: int = 15
    # ---------- G05:工具回执的有界视图与原始证据仓 ----------
    # 缺陷出处:回执经 json.dumps 后被按**字符**硬切(fold_output 对单行 JSON 只剩 hard_cap
    # 这条路),切点落在字符串或键值对中间 ⇒ 模型收到结构损坏的 JSON,而丢掉的那段从来没有
    # 落过盘。现在改成"在数据结构上裁 + 原文落盘可分页取回",这一条是**模型可见视图**的上限。
    # 默认 8000 = 与旧实现实际生效的那个数一致(不是收紧也不是放宽)。置 0 = 不裁(旧口径的
    # hard_cap=0),此时超大回执仍合法,只是把整个窗口吃掉。
    tool_visible_max_chars: int = 8000
    # 证据仓总开关。关闭后裁剪照做,但**原文取不回来**——这是有信息损失的开关,
    # 所以默认开,且属复现口径(SNAPSHOT_KEYS)而不是运维豁免。
    evidence_enabled: bool = True
    # 单个原始工件的体积上限:超了就不落盘(并在回执里明说 retrievable=false),
    # 而不是让一个巨型 diff 把磁盘吃穿
    evidence_max_item_bytes: int = 2_000_000
    # 一个任务的证据仓总量:满了同样明说,不静默覆盖旧证据
    evidence_max_total_bytes: int = 64_000_000
    # read_evidence 单页字节上限(模型可以要更少,要更多只会拿到这一页的天花板)
    evidence_page_bytes: int = 4000

    @field_validator(
        "tool_visible_max_chars", "evidence_max_item_bytes", "evidence_max_total_bytes"
    )
    @classmethod
    def _check_non_negative_sizes(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"size limits must be >= 0 (0 = 不限制), got {value}")
        return value

    @field_validator("evidence_page_bytes")
    @classmethod
    def _check_evidence_page_bytes(cls, value: int) -> int:
        if value < 256:
            raise ValueError(f"evidence_page_bytes must be >= 256, got {value}")
        return value

    # 自适应 Best-of-N(卡5):默认单线,仅在"同一断言连续 2 轮失败"或"补丁连续 2 次
    # 应用失败"时开一轮 2 候选并行择优,且每任务至多一次。置 false 即回到 V1 单线行为。
    adaptive_branching_enabled: bool = True
    branch_candidates: int = 2
    # 分支一次的开销约等于两轮 propose;余量低于此值就不分支(预算前置,不绕资源门禁)
    branching_min_token_reserve: int = 30_000
    # LOCALIZE 段的预算份额(占"任务剩余额度"的比例)。缺陷出处:该段此前拿的是整份
    # 任务余量,真实多文件题光靠只读调查就能花光预算而一次补丁不提
    # (实测 runs/swe-hard-graph 的 sphinx 两题:定位 16-19 轮、约 418k tokens、apply_patch 0 次)。
    # 置 1.0 = 旧行为(定位可花光剩余额度)。份额耗尽不等于任务级超支,后者仍是硬终点(N-5)。
    localize_budget_share: float = 0.6

    @field_validator("localize_budget_share")
    @classmethod
    def _check_localize_budget_share(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError(f"localize_budget_share must be in (0, 1], got {value}")
        return value

    # 四个概念互不替代(SPEC §5),键名与 app/agent/runtime.py 的 ContextPolicy 字段一一对应:
    # `context_window_tokens` = 软压缩目标(工作记忆阈值),`model_context_tokens` = 端点声明的
    # 单请求硬容量,`token_budget` = 任务累计额度,`llm_max_tokens` = 单次输出上限。
    # 工作记忆软阈值(M1 滑动窗口压缩):该段的 token 估算此前只增不减,
    # 真实多文件题光靠只读调查就把份额烧穿(runs/swe-hard-graph:16-19 轮、
    # 417,894 tok 撞 400k 顶、apply_patch 0 次)。超过此阈值时在 turn 边界压缩旧历史,
    # 而不是中止任务;**不放宽任何额度**——压完仍超预算照旧 BudgetError。
    # 默认 16000 的推导语义(不是实测结论,见 ADR-0004 的反方条目):
    # 一条 read_file 回执折叠后约 800-1000 tokens,`context_keep_recent_turns=6` 的
    # 不可压尾部约 6-7k,16k 意味着"超出在用尾巴约 9k 的历史"才开始让位——
    # 正好罩住实测那种 16-19 轮只读调查的形态,又不至于压掉模型当前正在看的东西。
    # 置 0 = 关闭,回到与压缩前逐字一致的行为(零成本对照口径)。
    context_window_tokens: int = 16_000
    # 尾部钉住最近多少个回合组不参与压缩:低于此值会把"模型正在用的观察"也压掉,
    # 表现为补丁阶段反复重读同一文件;高于此值则软阈值形同抬高
    context_keep_recent_turns: int = 6

    @field_validator("context_keep_recent_turns")
    @classmethod
    def _check_context_keep_recent_turns(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"context_keep_recent_turns must be >= 1, got {value}")
        return value

    # G07 运行 profile:legacy_v1 = 今日逐字(软阈值/钉住轮数/额度/输出上限全部照搬本文件),
    # repair_v2 = 目标软窗口 64k、尾部钉 12 轮,并且只在端点**声明**的容量允许时采用该目标
    # (装不下就夹小并留痕)。新默认切换要等 G16 收口,所以默认仍是 legacy_v1:历史批次身份不许
    # 被这次升级改写。
    agent_runtime_profile: str = "legacy_v1"

    @field_validator("agent_runtime_profile")
    @classmethod
    def _check_agent_runtime_profile(cls, value: str) -> str:
        normalized = value.strip() or "legacy_v1"
        if normalized not in {"legacy_v1", "repair_v2"}:
            raise ValueError(
                f"agent_runtime_profile must be 'legacy_v1' or 'repair_v2', got {value!r}"
            )
        return normalized

    # M6 崩溃恢复(A 级:循环工作记忆快照)。缺陷出处:`run_plain_loop` 的 messages/token
    # 计数是函数局部变量,一次 superstep 内跑到第 19 轮崩溃时 SqliteSaver 只有阶段边界的
    # 检查点,重放等于整个阶段冷启动重跑——已烧的 19 轮全部作废。置 true 时每个 turn 边界
    # 原子写一份 run_dir/loop_state.json,恢复从下一个 turn 续跑(轮次上限不重授)。
    # 它改变"恢复后的运行看到什么",故属 SNAPSHOT_KEYS(见 app/evals/provenance.py)。
    loop_snapshot_enabled: bool = True
    # M6 崩溃恢复(B 级):服务启动时,若僵尸任务留有可用快照则**重新入队续跑**,
    # 而不是按旧行为收敛 NEEDS_REVIEW。无快照/非 graph 引擎时行为与今日逐字相同。
    # 同样属 SNAPSHOT_KEYS:开与关得到的是两种任务终态,跨批次不可比。
    resume_on_restart: bool = True

    # 持久记忆(M2 仓库骨架):文件树 + Python 符号大纲由本地 ast 生成,零请求,注入
    # LOCALIZE/PROPOSE 的系统提示。缺陷证据(PROGRESS.md D.4/D.11):仓库结构**从未**进过
    # 提示,模型只能自己 list_files 现场重建;付费实跑 runs/swe-hard-graph* 的 sphinx 两题
    # 在 LOCALIZE 花 16-19 轮纯 read/search、417,894 tokens(撞 400k 份额顶)且 apply_patch
    # 0 次。骨架把"重新发现仓库"从模型的 turn 里拿走,让额度花在 bug 上。
    repo_map_enabled: bool = True
    # 骨架的字符预算(两档共用):0 = 不出骨架,与关闭等价
    repo_map_max_chars: int = 4000
    # 参与渲染的文件数上限(树与大纲共用同一批文件),超出部分计入 truncated 行
    repo_map_max_files: int = 200

    @field_validator("repo_map_max_chars")
    @classmethod
    def _check_repo_map_max_chars(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"repo_map_max_chars must be >= 0, got {value}")
        return value

    @field_validator("repo_map_max_files")
    @classmethod
    def _check_repo_map_max_files(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"repo_map_max_files must be >= 1, got {value}")
        return value

    # M2.5:大仓库(文件数 > repo_map_max_files)的结构档改出"目录级汇总",这个键是汇总的目录深度。
    # 深度 3 能让 sphinx/astropy 这类仓库用几十行讲完"有哪些去处",而不是把 1900 个文件裁成
    # 字母序前 200 个的半张地图(证据见 PROGRESS.md D.12)。
    repo_map_dir_depth: int = 3

    @field_validator("repo_map_dir_depth")
    @classmethod
    def _check_repo_map_dir_depth(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"repo_map_dir_depth must be >= 1, got {value}")
        return value

    # 检索引擎与结构化检索(M3 ACI 升级)。缺陷出处(PROGRESS.md D.6):检索是 Python 子串扫描
    # (app/tools/files.py 旧 :74-103),无上下文行、无单文件上限、无排序,全仓零 AST 能力;
    # 骨架给了地图却没有查地图的工具,付费实跑因此把 LOCALIZE 的 16-19 轮全花在 read/search 上
    # (runs/swe-hard-graph*:417,894 tokens、apply_patch 0 次)。
    # 三条路径的输出形状与顺序由 tests/test_search_tools.py 的等价性用例钉住,rg 缺席/失败/超时
    # 一律回落 Python(见 app/tools/search.py),故 rg 只是加速,不是第二种语义。
    search_engine: str = "auto"  # auto=有 rg 就用 rg | python=只用内置扫描 | rg=优先 rg(缺席仍回落)
    # 模型可见上下文的最大长度,不是性能参数:context_lines 传 10000 时必须被夹扣而不是照给
    # (夹扣不报错——模型不会因为只拿到 5 行就崩,拿到 5000 行才会把整个额度吃掉)
    max_search_context_lines: int = 5
    # find_symbol 的返回条数上限:同名符号在前端仓库能出上百条,不裁就是一次上下文尖峰
    max_symbol_results: int = 30

    @field_validator("search_engine")
    @classmethod
    def _check_search_engine(cls, value: str) -> str:
        if value not in {"auto", "python", "rg"}:
            raise ValueError(f"search_engine must be 'auto', 'python' or 'rg', got {value!r}")
        return value

    @field_validator("max_search_context_lines")
    @classmethod
    def _check_max_search_context_lines(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"max_search_context_lines must be >= 0, got {value}")
        return value

    @field_validator("max_symbol_results")
    @classmethod
    def _check_max_symbol_results(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_symbol_results must be >= 1, got {value}")
        return value

    # M3.6:检索与符号查找的**遍历域**上限,与 `list_files` 的输出体量上限(MAX_LIST_FILES=500)
    # 解耦。缺陷证据(PROGRESS.md D.12,用平台自己的口径在真实题面仓库上量过):两者共用 500 时,
    # 排序第 500 个之后的文件对模型完全不可见——9 道已缓存难题里 4 道的必改文件落在范围外
    # (sphinx-7590 的 sphinx/util/cfamily.py=631、scikit-learn-12682 的两处=502/903),
    # 而"搜不到"会被模型读成"这里没有",于是反复重查、把额度烧光。
    # 8000 覆盖本项目跑过的最大仓库(astropy 1924 文件)并留出余量;裁过时工具会如实给
    # scope_truncated=True,不给假称穷尽仓库的空结论。
    max_search_files: int = 8000

    @field_validator("max_search_files")
    @classmethod
    def _check_max_search_files(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_search_files must be >= 1, got {value}")
        return value

    # ---------- G06:连续会话与结构化任务记忆 ----------
    # 缺陷出处:阶段之间只传一段模型自述的 findings 文本,读过的行范围、反例、已排除方向
    # 全部落在阶段边界之外(付费实跑 runs/swe-hard-graph2/3:PROPOSE 13 次 search / 10 次 read、
    # apply_patch 0 次 —— 拿着摘要重新调查)。记忆块决定"下一阶段的请求里到底有没有这些证据"。
    task_memory_enabled: bool = True
    # 记忆块的字符预算(与 repo_map_max_chars 同族:它改变模型可见上下文本身,不是运维参数)
    task_memory_max_chars: int = 3000

    @field_validator("task_memory_max_chars")
    @classmethod
    def _check_task_memory_max_chars(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"task_memory_max_chars must be >= 0, got {value}")
        return value

    # ---------- G08:目标仓库自带说明(AGENTS.md 一类)的加载与冻结口径 ----------
    # 缺陷出处:被诊断仓库里写给 Agent 看的规则从来没进过请求,模型只能靠撞上门禁才知道
    # "这个仓库不许动 tests/"。开关关掉 ⇒ 装配面与接入之前逐字节相同(零成本对照口径)。
    repo_instructions_enabled: bool = True
    # 单份说明的字节档:超了就截断,并在块里明写"还有 N 字节未显示"(不许静默丢约束)
    repo_instructions_max_file_bytes: int = 16_384
    # 一个任务能纳入的说明总量:满了就把整份列进"未纳入",块末照样点名报出
    repo_instructions_max_total_bytes: int = 65_536

    @field_validator("repo_instructions_max_file_bytes", "repo_instructions_max_total_bytes")
    @classmethod
    def _check_instruction_sizes(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"repo_instructions size must be >= 0, got {value}")
        return value

    # PLAN 阶段(M5,范式 LOCALIZE→PLAN→ACT→VERIFY)。缺陷出处(PROGRESS.md D.8/D.11):
    # 计划从来不是工件——"计划如何修复"只是 LOCALIZE_PROMPT 要求塞进 finish.summary 的自由文本,
    # 付费实跑 runs/swe-hard-graph/3 两次独立跑法同形:PROPOSE 13 次 search / 10 次 read、
    # apply_patch 0 次(拿着薄摘要重新调查,而不是按计划动手)。
    # 置 false = 逐字退回旧行为:不发计划请求、不记轨迹、state.plan 恒空,
    # 于是 PROPOSE 的渲染与引入本阶段之前逐字节相同。
    plan_stage_enabled: bool = True
    # 计划段占"任务剩余额度"的比例(与 localize_budget_share 同一口径,share 只切余量、
    # 不放宽任何额度)。计划只产出一段文本,份额应当远小于定位;
    # 份额耗尽属于**降级**(带暂定文本继续 propose),任务级总额耗尽仍是硬终点(N-5)。
    plan_budget_share: float = 0.15

    @field_validator("plan_budget_share")
    @classmethod
    def _check_plan_budget_share(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError(f"plan_budget_share must be in (0, 1], got {value}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
