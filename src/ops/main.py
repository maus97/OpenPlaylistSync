"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from ops.models import SyncPair
from ops.providers.errors import diagnostic
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
from ops.sync.leases import PairOperationBusy, acquire_pair_lease
from ops.sync.scheduling import evaluate_pair


def _lifespan(base_settings: Settings, *, load_gui_settings: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_settings = base_settings
        if load_gui_settings:
            with SessionLocal() as session:
                active_settings = load_app_settings(session, base_settings)
        ops_logger = logging.getLogger("ops")
        if not ops_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            ops_logger.addHandler(handler)
        ops_logger.setLevel(getattr(logging, active_settings.log_level.upper(), logging.INFO))
        ops_logger.propagate = False
        app.state.security_mode.https_enabled = active_settings.https_mode_enabled
        scheduler = SchedulerService(active_settings, sync_job=run_scheduled_sync)
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown()

    return lifespan


def run_scheduled_sync() -> bool:
    """Preview by default; apply only for explicitly opted-in playlist pairs."""

    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as session:
            settings = load_app_settings(session)
            if not settings.scheduler_enabled or not settings.credential_encryption_key:
                logger.info("Scheduler paused: disabled or credential storage unavailable")
                return True
            pair_ids = [p.id for p in SyncPairRepository(session).get_enabled()]
    except Exception as exc:
        logger.error("Scheduler enumeration failed; next tick will retry: %s", diagnostic(exc))
        return False
    for pair_id in pair_ids:
        lease = None
        try:
            with SessionLocal() as session:
                pair = session.get(SyncPair, pair_id)
                if pair is None or not pair.enabled:
                    continue
                lease = acquire_pair_lease(session, pair.id, kind="automatic")
                coordinator = SyncCoordinator(session, settings, create_provider)
                try:
                    outcome = evaluate_pair(coordinator, pair)
                    logger.info(
                        "Scheduled pair %s: %s; next evaluation %s UTC",
                        pair_id,
                        outcome,
                        pair.automatic_next_at,
                    )
                except Exception as exc:
                    session.rollback()
                    info = diagnostic(exc)
                    pair = session.get(SyncPair, pair_id, populate_existing=True)
                    pair.automatic_outcome = (
                        "Another operation is running; retry next tick"
                        if isinstance(exc, PairOperationBusy)
                        else info["error"]
                    )
                    pair.automatic_next_at = datetime.now(UTC) + timedelta(
                        minutes=settings.sync_interval_minutes
                    )
                    session.commit()
                    logger.warning(
                        "Scheduled pair %s failed; next tick will retry: %s", pair_id, info
                    )
        except PairOperationBusy:
            logger.info("Scheduled pair %s skipped: automatic job already running", pair_id)
        except Exception as exc:
            logger.error(
                "Scheduled pair %s boundary failed; next tick will retry: %s",
                pair_id,
                diagnostic(exc),
            )
        finally:
            if lease:
                try:
                    lease.release()
                except Exception as exc:
                    logger.error(
                        "Scheduled pair %s lease release failed; lease expires: %s",
                        pair_id,
                        diagnostic(exc),
                    )
    return True


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
