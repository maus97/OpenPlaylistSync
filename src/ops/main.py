"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ops import __version__
from ops.api.routes import router
from ops.config import Settings, get_settings
from ops.configuration import load_app_settings
from ops.db import SessionLocal
from ops.providers.factory import create_provider
from ops.scheduler import SchedulerService
from ops.security.logging import install_sensitive_query_filter
from ops.security.middleware import (
    LocalAuthenticationMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
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


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application."""

    settings = app_settings or get_settings()
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError("a session secret of at least 32 characters is required")
    secure_cookie = (
        settings.session_cookie_secure
        if settings.session_cookie_secure is not None
        else settings.environment == "production"
    )
    install_sensitive_query_filter()
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
    app.add_middleware(LocalAuthenticationMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret or "",
        session_cookie="ops_session",
        https_only=secure_cookie,
        same_site="lax",
        max_age=8 * 60 * 60,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=bool(secure_cookie and settings.environment == "production"),
    )
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    """Run the development/standalone server."""

    uvicorn.run(
        "ops.main:app",
        host="127.0.0.1",
        port=8000,
        proxy_headers=False,
        server_header=False,
    )
