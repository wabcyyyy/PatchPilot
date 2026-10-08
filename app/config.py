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
