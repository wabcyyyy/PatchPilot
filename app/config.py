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


@lru_cache
def get_settings() -> Settings:
    return Settings()
