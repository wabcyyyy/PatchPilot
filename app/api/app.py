"""FastAPI 应用工厂:lifespan 里完成启动恢复与优雅停机。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.routes import router
from app.api.service import TaskService
from app.config import get_settings
from app.storage.repository import Repository


def create_app(
    db_path: Path | str | None = None,
    runs_root: Path | str | None = None,
    bugs_root: Path | str | None = None,
) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        repo = Repository(db_path or settings.db_path)
        service = TaskService(
            repo=repo,
            runs_root=Path(runs_root) if runs_root else None,
            bugs_root=bugs_root or Path("bugs"),
        )
        service.recover_stale()
        app.state.service = service
        yield
        service.shutdown()

    app = FastAPI(title="PatchPilot", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
