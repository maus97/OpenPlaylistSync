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
    RuntimeSecurityMode,
    SecurityHeadersMiddleware,
)
from ops.storage.repositories import SyncPairRepository
from ops.sync.coordinator import SyncCoordinator


def _lifespan(base_settings: Settings, *, load_gui_settings: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_settings = base_settings
        if load_gui_settings:
            with SessionLocal() as session:
                active_settings = load_app_settings(session, base_settings)
        app.state.security_mode.https_enabled = active_settings.https_mode_enabled
        scheduler = SchedulerService(active_settings, sync_job=run_scheduled_sync)
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown()

    return lifespan


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
    load_gui_settings = app_settings is None
    if not settings.session_secret or len(settings.session_secret) < 32:
        raise RuntimeError("a session secret of at least 32 characters is required")
    security_mode = RuntimeSecurityMode(https_enabled=settings.https_mode_enabled)
    install_sensitive_query_filter()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=_lifespan(settings, load_gui_settings=load_gui_settings),
    )
    app.state.security_mode = security_mode
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
        https_only=False,
        same_site="lax",
        max_age=8 * 60 * 60,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(
        SecurityHeadersMiddleware,
        security_mode=security_mode,
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
