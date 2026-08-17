"""HTTP routes for health, operator flows, and the safety-first UI."""

import os
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
from ops.db import get_db
from ops.models import ProviderAccount, SyncBaseline, SyncPair, SyncRun
from ops.providers.demo import mutate_demo_state, reset_demo_state
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
templates = Jinja2Templates(
    directory=os.environ.get(
        "OPS_TEMPLATES_DIR", str(Path(__file__).resolve().parents[3] / "templates")
    )
)


def settings() -> Settings:
    return get_settings()


def provider_for_account(session: Session, app_settings: Settings, account: ProviderAccount) -> Any:
    """Build a provider for UI discovery without exposing credential payloads."""

    if account.provider_name.startswith("demo_"):
        return create_provider(account, {})
    if not app_settings.credential_encryption_key:
        raise ValueError("credential encryption is not configured")
    cipher = CredentialCipher(app_settings.credential_encryption_key)
    credentials = ProviderAccountRepository(session, cipher).load_credentials(account)
    return create_provider(account, credentials)


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


@router.post("/demo/seed", response_class=RedirectResponse, include_in_schema=False)
def seed_demo_data(session: Annotated[Session, Depends(get_db)]) -> RedirectResponse:
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
    session.execute(delete(SyncRun).where(SyncRun.baseline_id.in_(baseline_ids)))
    session.execute(delete(SyncBaseline).where(SyncBaseline.pair_id == demo_pair.id))
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/demo/change", response_class=RedirectResponse, include_in_schema=False)
def change_demo_data(change: Annotated[str, Form()]) -> RedirectResponse:
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
def pairs(request: Request, session: Annotated[Session, Depends(get_db)]) -> HTMLResponse:
    app_settings = get_settings()
    accounts = list(session.scalars(select(ProviderAccount).order_by(ProviderAccount.id)))
    playlist_options = []
    for account in accounts:
        try:
            provider = provider_for_account(session, app_settings, account)
            playlists = provider.list_playlists()
        except Exception:
            playlists = ()
        playlist_options.append({"account": account, "playlists": playlists})
    configured_pairs = SyncPairRepository(session).all()
    return templates.TemplateResponse(
        request=request,
        name="pairs.html",
        context={
            "accounts": accounts,
            "pairs": configured_pairs,
            "playlist_options": playlist_options,
        },
    )


@router.post("/pairs", response_class=RedirectResponse, include_in_schema=False)
def create_pair(
    source_selection: Annotated[str, Form()],
    target_selection: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
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


@router.post("/sync/baseline/{pair_id}", response_class=RedirectResponse, include_in_schema=False)
def establish_sync_baseline(
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
) -> RedirectResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        SyncCoordinator(session, app_settings, create_provider).establish_baseline(pair)
    except (ValueError, CredentialEncryptionError) as exc:
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
