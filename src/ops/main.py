"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ops import __version__
from ops.api.routes import router
from ops.config import get_settings
from ops.configuration import load_app_settings
from ops.db import SessionLocal
from ops.providers.factory import create_provider
from ops.scheduler import SchedulerService
from ops.storage.repositories import SyncPairRepository
from ops.sync.coordinator import SyncCoordinator


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = SchedulerService(get_settings(), sync_job=run_scheduled_sync)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown()


def run_scheduled_sync() -> None:
    """Create safe preview plans for enabled pairs; never apply writes automatically."""

    with SessionLocal() as session:
        settings = load_app_settings(session)
        if not settings.credential_encryption_key:
            return
        coordinator = SyncCoordinator(session, settings, create_provider)
        for pair in SyncPairRepository(session).get_enabled():
            coordinator.preview(pair)


def create_app() -> FastAPI:
    """Create the FastAPI application."""

    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(
            directory=os.environ.get(
                "OPS_STATIC_DIR", str(Path(__file__).resolve().parents[2] / "static")
            )
        ),
        name="static",
    )
    if settings.session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            https_only=(
                settings.session_cookie_secure
                if settings.session_cookie_secure is not None
                else settings.environment == "production"
            ),
        )
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    """Run the development/standalone server."""

    uvicorn.run("ops.main:app", host="0.0.0.0", port=8000)
