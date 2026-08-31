"""HTTP routes for health, operator flows, and the safety-first UI."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService
from ops.config import Settings, get_settings
from ops.configuration import load_app_settings, load_saved_settings, save_app_settings
from ops.db import get_db
from ops.models import ProviderAccount, SyncAction, SyncBaseline, SyncPair, SyncRun
from ops.providers.base import ProviderError
from ops.providers.demo import mutate_demo_state, reset_demo_state
from ops.providers.factory import create_provider
from ops.security.crypto import CredentialCipher, CredentialEncryptionError
from ops.security.csrf import csrf_context, require_csrf
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncPairRepository,
    SyncRunRepository,
)
from ops.sync.coordinator import SyncCoordinator
from ops.sync.domain import InitialSyncPolicy
from ops.sync.executor import PlanExecutionError
from ops.sync.safety import Approval, DestructiveActionApprovalError, plan_fingerprint

router = APIRouter()
templates = Jinja2Templates(
    directory=os.environ.get(
        "OPS_TEMPLATES_DIR", str(Path(__file__).resolve().parents[3] / "templates")
    )
)
templates.context_processors.append(csrf_context)


def settings(session: Annotated[Session, Depends(get_db)]) -> Settings:
    return load_app_settings(session)


def provider_for_account(session: Session, app_settings: Settings, account: ProviderAccount) -> Any:
    """Build a provider for UI discovery without exposing credential payloads."""

    if account.provider_name.startswith("demo_"):
        return create_provider(account, {})
    if not app_settings.credential_encryption_key:
        raise ValueError("credential encryption is not configured")
    cipher = CredentialCipher(app_settings.credential_encryption_key)
    credentials = ProviderAccountRepository(session, cipher).load_credentials(account)
    if account.provider_name == "spotify" and credentials.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(str(credentials["expires_at"])).astimezone(UTC)
        except ValueError:
            expires_at = datetime.now(UTC)
        if expires_at <= datetime.now(UTC):
            refresh_token = credentials.get("refresh_token")
            if (
                not refresh_token
                or not app_settings.spotify_client_id
                or not app_settings.spotify_client_secret
            ):
                raise ValueError("Spotify authorization expired; reconnect the account")
            refreshed = SpotifyOAuthService(
                SpotifyOAuthConfig(
                    app_settings.spotify_client_id,
                    app_settings.spotify_client_secret,
                    app_settings.spotify_redirect_uri,
                )
            ).refresh_token(str(refresh_token))
            credentials = {
                **credentials,
                **refreshed,
                "refresh_token": refreshed.get("refresh_token", refresh_token),
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
                ).isoformat(),
            }
            ProviderAccountRepository(session, cipher).save_credentials(account, credentials)
            session.commit()
    if account.provider_name == "youtube_music":
        credentials = {
            **credentials,
            "_oauth_client_id": app_settings.ytmusic_client_id,
            "_oauth_client_secret": app_settings.ytmusic_client_secret,
        }
    return create_provider(account, credentials)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, app_settings: Annotated[Settings, Depends(settings)]) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": app_settings.app_name, "environment": app_settings.environment},
    )


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    saved: str | None = None,
) -> HTMLResponse:
    """Render the operator configuration screen without revealing secrets."""

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "saved": saved == "1",
            "spotify_client_id": app_settings.spotify_client_id or "",
            "spotify_redirect_uri": app_settings.spotify_redirect_uri,
            "spotify_secret_saved": bool(app_settings.spotify_client_secret),
            "ytmusic_client_id": app_settings.ytmusic_client_id or "",
            "ytmusic_secret_saved": bool(app_settings.ytmusic_client_secret),
            "scheduler_enabled": app_settings.scheduler_enabled,
            "sync_interval_minutes": app_settings.sync_interval_minutes,
        },
    )


@router.post("/settings", response_class=RedirectResponse, include_in_schema=False)
def save_settings_route(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    spotify_client_id: Annotated[str, Form()] = "",
    spotify_client_secret: Annotated[str, Form()] = "",
    spotify_redirect_uri: Annotated[str, Form()] = "",
    clear_spotify_secret: Annotated[str | None, Form()] = None,
    ytmusic_client_id: Annotated[str, Form()] = "",
    ytmusic_client_secret: Annotated[str, Form()] = "",
    clear_ytmusic_secret: Annotated[str | None, Form()] = None,
    scheduler_enabled: Annotated[str | None, Form()] = None,
    sync_interval_minutes: Annotated[int, Form()] = 60,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Encrypt and save provider and scheduler settings submitted by the UI."""

    base_settings = get_settings()
    saved = load_saved_settings(session, base_settings)

    def existing_secret(key: str) -> str:
        if key in saved:
            return str(saved[key] or "")
        return str(getattr(base_settings, key) or "")

    values = {
        "spotify_client_id": spotify_client_id.strip(),
        "spotify_client_secret": (
            ""
            if clear_spotify_secret is not None
            else spotify_client_secret.strip() or existing_secret("spotify_client_secret")
        ),
        "spotify_redirect_uri": spotify_redirect_uri.strip() or base_settings.spotify_redirect_uri,
        "ytmusic_client_id": ytmusic_client_id.strip(),
        "ytmusic_client_secret": (
            ""
            if clear_ytmusic_secret is not None
            else ytmusic_client_secret.strip() or existing_secret("ytmusic_client_secret")
        ),
        "scheduler_enabled": scheduler_enabled is not None,
        "sync_interval_minutes": max(1, min(sync_interval_minutes, 1440)),
    }
    save_app_settings(session, values, base_settings)
    session.commit()

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.reconfigure(load_app_settings(session, base_settings))
    return RedirectResponse("/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/healthz", tags=["system"])
def healthz() -> dict[str, str]:
    """Return a cheap process health response without requiring provider access."""

    return {"status": "ok", "service": "open-playlist-sync"}


@router.get("/auth/spotify/start", include_in_schema=False)
def spotify_start(
    request: Request, app_settings: Annotated[Settings, Depends(settings)]
) -> RedirectResponse:
    if not all(
        (
            app_settings.session_secret,
            app_settings.spotify_client_id,
            app_settings.spotify_client_secret,
        )
    ):
        raise HTTPException(status_code=503, detail="Spotify OAuth is not configured")
    service = SpotifyOAuthService(
        SpotifyOAuthConfig(
            client_id=app_settings.spotify_client_id,
            client_secret=app_settings.spotify_client_secret,
            redirect_uri=app_settings.spotify_redirect_uri,
        )
    )
    authorization_url, state = service.authorization_url()
    request.session["spotify_oauth_state"] = state
    return RedirectResponse(authorization_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/auth/spotify/callback", include_in_schema=False)
def spotify_callback(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        request.session.pop("spotify_oauth_state", None)
        return RedirectResponse(
            f"/pairs?connection_error=spotify_{error}", status_code=status.HTTP_303_SEE_OTHER
        )
    expected_state = request.session.pop("spotify_oauth_state", None)
    if not code or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="invalid Spotify OAuth state")
    if not all(
        (
            app_settings.spotify_client_id,
            app_settings.spotify_client_secret,
            app_settings.credential_encryption_key,
        )
    ):
        raise HTTPException(status_code=503, detail="Spotify credential storage is not configured")
    service = SpotifyOAuthService(
        SpotifyOAuthConfig(
            client_id=app_settings.spotify_client_id,
            client_secret=app_settings.spotify_client_secret,
            redirect_uri=app_settings.spotify_redirect_uri,
        )
    )
    token = service.exchange_code(code)
    token["expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=int(token.get("expires_in", 3600)))
    ).isoformat()
    profile = service.current_user(token["access_token"])
    account_repo = ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key)
    )
    account = account_repo.get_by_external_id("spotify", profile["id"])
    if account is None:
        account = ProviderAccount(provider_name="spotify", external_account_id=profile["id"])
    account_repo.save_credentials(account, token)
    session.commit()
    return RedirectResponse("/pairs?connected=spotify", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/youtube_music/start", response_class=HTMLResponse, include_in_schema=False)
def youtube_music_start(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
) -> HTMLResponse:
    if not all(
        (
            app_settings.session_secret,
            app_settings.ytmusic_client_id,
            app_settings.ytmusic_client_secret,
        )
    ):
        raise HTTPException(status_code=503, detail="YouTube Music OAuth is not configured")
    service = YouTubeMusicAuthService(
        app_settings.ytmusic_client_id,
        app_settings.ytmusic_client_secret,
    )
    auth_code = service.request_code()
    request.session["ytmusic_device_code"] = auth_code["device_code"]
    return templates.TemplateResponse(
        request=request,
        name="youtube_music_auth.html",
        context={"auth_code": auth_code},
    )


@router.get("/auth/youtube_music/complete", include_in_schema=False)
def youtube_music_complete(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
) -> RedirectResponse:
    device_code = request.session.pop("ytmusic_device_code", None)
    if not device_code:
        raise HTTPException(status_code=400, detail="no pending YouTube Music authorization")
    if not all(
        (
            app_settings.ytmusic_client_id,
            app_settings.ytmusic_client_secret,
            app_settings.credential_encryption_key,
        )
    ):
        raise HTTPException(
            status_code=503, detail="YouTube Music credential storage is not configured"
        )
    service = YouTubeMusicAuthService(
        app_settings.ytmusic_client_id,
        app_settings.ytmusic_client_secret,
    )
    token = service.exchange_device_code(device_code)
    account_repo = ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key)
    )
    account = account_repo.get_by_external_id("youtube_music", "default")
    if account is None:
        account = ProviderAccount(provider_name="youtube_music", external_account_id="default")
    account_repo.save_credentials(account, token)
    session.commit()
    return RedirectResponse("/pairs?connected=youtube_music", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/demo/seed", response_class=RedirectResponse, include_in_schema=False)
def seed_demo_data(
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Create local synthetic accounts and a pair for immediate GUI testing."""

    reset_demo_state()
    account_ids: dict[str, int] = {}
    for provider_name in ("demo_spotify", "demo_youtube_music"):
        account = session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.provider_name == provider_name,
                ProviderAccount.external_account_id == "local-demo-user",
            )
        )
        if account is None:
            account = ProviderAccount(
                provider_name=provider_name,
                external_account_id="local-demo-user",
            )
            session.add(account)
            session.flush()
        account_ids[provider_name] = account.id
    pair_repo = SyncPairRepository(session)
    demo_pair = next(
        (
            pair
            for pair in pair_repo.all()
            if pair.source_account_id == account_ids["demo_spotify"]
            and pair.target_account_id == account_ids["demo_youtube_music"]
        ),
        None,
    )
    if demo_pair is None:
        demo_pair = SyncPair(
            source_account_id=account_ids["demo_spotify"],
            target_account_id=account_ids["demo_youtube_music"],
            source_playlist_id="spotify:demo-playlist",
            target_playlist_id="youtube_music:demo-playlist",
        )
        session.add(demo_pair)
        session.flush()
    baseline_ids = select(SyncBaseline.id).where(SyncBaseline.pair_id == demo_pair.id)
    run_ids = select(SyncRun.id).where(SyncRun.baseline_id.in_(baseline_ids))
    session.execute(delete(SyncAction).where(SyncAction.run_id.in_(run_ids)))
    session.execute(delete(SyncRun).where(SyncRun.baseline_id.in_(baseline_ids)))
    session.execute(delete(SyncBaseline).where(SyncBaseline.pair_id == demo_pair.id))
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/demo/change", response_class=RedirectResponse, include_in_schema=False)
def change_demo_data(
    change: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    mutate_demo_state(change)
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/runs", response_class=HTMLResponse, include_in_schema=False)
def recent_runs(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    runs = SyncRunRepository(session).recent()
    return templates.TemplateResponse(request=request, name="runs.html", context={"runs": runs})


@router.get("/pairs", response_class=HTMLResponse, include_in_schema=False)
def pairs(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    connected: str | None = None,
    connection_error: str | None = None,
) -> HTMLResponse:
    app_settings = load_app_settings(session)
    accounts = list(session.scalars(select(ProviderAccount).order_by(ProviderAccount.id)))
    connection_status = {
        "spotify": all(
            (
                app_settings.session_secret,
                app_settings.credential_encryption_key,
                app_settings.spotify_client_id,
                app_settings.spotify_client_secret,
            )
        ),
        "youtube_music": all(
            (
                app_settings.session_secret,
                app_settings.credential_encryption_key,
                app_settings.ytmusic_client_id,
                app_settings.ytmusic_client_secret,
            )
        ),
    }
    connection_message = {
        "spotify": "Spotify connected. Choose playlists below to create a pair.",
        "youtube_music": "YouTube Music connected. Choose playlists below to create a pair.",
    }.get(connected)
    if connection_error:
        connection_message = "Connection was cancelled or rejected. You can safely try again."
    playlist_options = []
    provider_errors: dict[int, str] = {}
    for account in accounts:
        try:
            provider = provider_for_account(session, app_settings, account)
            playlists = provider.list_playlists()
        except (ProviderError, ValueError) as exc:
            playlists = ()
            provider_errors[account.id] = str(exc)
        playlist_options.append({"account": account, "playlists": playlists})
    configured_pairs = SyncPairRepository(session).all()
    return templates.TemplateResponse(
        request=request,
        name="pairs.html",
        context={
            "accounts": accounts,
            "pairs": configured_pairs,
            "playlist_options": playlist_options,
            "connection_status": connection_status,
            "connection_message": connection_message,
            "provider_errors": provider_errors,
        },
    )


@router.post("/pairs", response_class=RedirectResponse, include_in_schema=False)
def create_pair(
    source_selection: Annotated[str, Form()],
    target_selection: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
    initial_sync_policy: Annotated[str, Form()] = InitialSyncPolicy.MERGE.value,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    try:
        source_account_raw, source_playlist_id = source_selection.split("|", 1)
        target_account_raw, target_playlist_id = target_selection.split("|", 1)
        source_account_id = int(source_account_raw)
        target_account_id = int(target_account_raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid playlist selection") from exc
    if source_account_id == target_account_id:
        raise HTTPException(status_code=400, detail="source and target accounts must differ")
    source_account = session.get(ProviderAccount, source_account_id)
    target_account = session.get(ProviderAccount, target_account_id)
    if source_account is None or target_account is None:
        raise HTTPException(status_code=404, detail="provider account not found")
    try:
        policy = InitialSyncPolicy(initial_sync_policy)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="invalid initial synchronization policy"
        ) from exc
    session.add(
        SyncPair(
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            source_playlist_id=source_playlist_id,
            target_playlist_id=target_playlist_id,
            initial_sync_policy=policy.value,
        )
    )
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/pairs/{pair_id}/toggle", response_class=RedirectResponse, include_in_schema=False)
def toggle_pair(
    pair_id: int,
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    pair.enabled = not pair.enabled
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/pairs/{pair_id}/delete", response_class=RedirectResponse, include_in_schema=False)
def delete_pair(
    pair_id: int,
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    baseline_ids = select(SyncBaseline.id).where(SyncBaseline.pair_id == pair.id)
    run_ids = select(SyncRun.id).where(
        (SyncRun.pair_id == pair.id) | (SyncRun.baseline_id.in_(baseline_ids))
    )
    session.execute(delete(SyncAction).where(SyncAction.run_id.in_(run_ids)))
    session.execute(delete(SyncRun).where(SyncRun.id.in_(run_ids)))
    session.execute(delete(SyncBaseline).where(SyncBaseline.pair_id == pair.id))
    session.delete(pair)
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/accounts/{provider_name}/disconnect",
    response_class=RedirectResponse,
    include_in_schema=False,
)
def disconnect_provider(
    provider_name: str,
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    account = session.scalar(
        select(ProviderAccount)
        .where(ProviderAccount.provider_name == provider_name)
        .order_by(ProviderAccount.updated_at.desc(), ProviderAccount.id.desc())
        .limit(1)
    )
    if account is None:
        raise HTTPException(status_code=404, detail="provider account not found")
    account.credentials_ciphertext = None
    account.credential_key_id = None
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/sync/plan/{pair_id}", response_class=HTMLResponse, include_in_schema=False)
def sync_plan(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        plan = SyncCoordinator(session, app_settings, create_provider).preview(pair)
    except (ValueError, CredentialEncryptionError, ProviderError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request=request,
        name="sync_plan.html",
        context={"pair": pair, "plan": plan, "fingerprint": plan_fingerprint(plan), "error": None},
    )


@router.post("/sync/baseline/{pair_id}", response_class=RedirectResponse, include_in_schema=False)
def establish_sync_baseline(
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        SyncCoordinator(session, app_settings, create_provider).accept_current_state(pair)
    except (ValueError, CredentialEncryptionError, ProviderError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(f"/sync/plan/{pair_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sync/apply/{pair_id}", response_class=HTMLResponse, include_in_schema=False)
def apply_sync_plan(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    confirmation: Annotated[str, Form()] = "",
    fingerprint: Annotated[str, Form()] = "",
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    plan = None
    error = None
    try:
        coordinator = SyncCoordinator(session, app_settings, create_provider)
        plan = coordinator.preview(pair)
        coordinator.apply(
            pair,
            plan,
            Approval(plan_fingerprint=fingerprint, confirmation=confirmation),
        )
    except (
        ValueError,
        CredentialEncryptionError,
        DestructiveActionApprovalError,
        PlanExecutionError,
        ProviderError,
    ) as exc:
        error = str(exc)
    if error is None:
        return RedirectResponse(f"/sync/plan/{pair_id}", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="sync_plan.html",
        context={
            "pair": pair,
            "plan": plan,
            "fingerprint": fingerprint,
            "error": error,
        },
    )
