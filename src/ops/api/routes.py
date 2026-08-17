"""HTTP routes for health, operator flows, and the safety-first UI."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService
from ops.config import Settings, get_settings
from ops.db import get_db
from ops.models import ProviderAccount, SyncPair
from ops.providers.factory import create_provider
from ops.security.crypto import CredentialCipher, CredentialEncryptionError
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncPairRepository,
    SyncRunRepository,
)
from ops.sync.coordinator import SyncCoordinator
from ops.sync.safety import Approval, DestructiveActionApprovalError, plan_fingerprint

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


def settings() -> Settings:
    return get_settings()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, app_settings: Annotated[Settings, Depends(settings)]) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": app_settings.app_name, "environment": app_settings.environment},
    )


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
    code: str,
    state: str,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    expected_state = request.session.pop("spotify_oauth_state", None)
    if not expected_state or state != expected_state:
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
    profile = service.current_user(token["access_token"])
    account_repo = ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key)
    )
    account = account_repo.get_by_external_id("spotify", profile["id"])
    if account is None:
        account = ProviderAccount(provider_name="spotify", external_account_id=profile["id"])
    account_repo.save_credentials(account, token)
    session.commit()
    return {"status": "authenticated", "provider": "spotify", "account_id": profile["id"]}


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
) -> dict[str, str]:
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
    return {"status": "authenticated", "provider": "youtube_music", "account_id": "default"}


@router.get("/runs", response_class=HTMLResponse, include_in_schema=False)
def recent_runs(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    runs = SyncRunRepository(session).recent()
    return templates.TemplateResponse(request=request, name="runs.html", context={"runs": runs})


@router.get("/pairs", response_class=HTMLResponse, include_in_schema=False)
def pairs(request: Request, session: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    accounts = list(session.query(ProviderAccount).order_by(ProviderAccount.id))
    configured_pairs = SyncPairRepository(session).all()
    return templates.TemplateResponse(
        request=request,
        name="pairs.html",
        context={"accounts": accounts, "pairs": configured_pairs},
    )


@router.post("/pairs", response_class=RedirectResponse, include_in_schema=False)
def create_pair(
    source_account_id: Annotated[int, Form()],
    target_account_id: Annotated[int, Form()],
    source_playlist_id: Annotated[str, Form()],
    target_playlist_id: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
) -> RedirectResponse:
    if source_account_id == target_account_id:
        raise HTTPException(status_code=400, detail="source and target accounts must differ")
    source_account = session.get(ProviderAccount, source_account_id)
    target_account = session.get(ProviderAccount, target_account_id)
    if source_account is None or target_account is None:
        raise HTTPException(status_code=404, detail="provider account not found")
    session.add(
        SyncPair(
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            source_playlist_id=source_playlist_id,
            target_playlist_id=target_playlist_id,
        )
    )
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
    except (ValueError, CredentialEncryptionError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request=request,
        name="sync_plan.html",
        context={"pair": pair, "plan": plan, "fingerprint": plan_fingerprint(plan), "error": None},
    )


@router.post("/sync/apply/{pair_id}", response_class=HTMLResponse, include_in_schema=False)
def apply_sync_plan(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    confirmation: Annotated[str, Form()] = "",
    fingerprint: Annotated[str, Form()] = "",
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
    except (ValueError, CredentialEncryptionError, DestructiveActionApprovalError) as exc:
        error = str(exc)
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
