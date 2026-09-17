"""全局配置:pydantic-settings,环境变量前缀 PATCHPILOT_,支持 .env。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PATCHPILOT_", env_file=".env", extra="ignore")

    # 目录
    runs_root: Path = Path("runs")
    workspace_root: Path = Path("runs/workspaces")
    db_path: Path = Path("patchpilot.sqlite3")

    # 模型(OpenAI 兼容端点;留空则仅回放模式可用)
    llm_enabled: bool = False  # 真实 LLM 调用总开关,默认关闭以防误配 key 即产生花费
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 4096  # 单次 completion 输出上限;0 = 不限制
    llm_timeout_seconds: float = 120  # 单次请求超时(SDK 默认 600s,过长会拖垮任务)
    llm_max_retries: int = 1

    # 基础设施(可选)
    redis_url: str = ""
    docker_image: str = "python:3.11-slim"

    # 预算与限制(企划书第 9 节资源门禁的默认值)
    token_budget: int = 200_000  # 单任务累计 token 预算;0 = 不限制
    default_max_rounds: int = 5
    task_timeout_seconds: int = 900
    round_timeout_seconds: int = 300
    test_timeout_seconds: int = 120
    max_patch_files: int = 5
    max_read_lines: int = 400
    max_search_results: int = 50
    max_output_chars: int = 20_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
