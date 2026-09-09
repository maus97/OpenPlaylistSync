"""HTTP routes for health, operator flows, and the safety-first UI."""

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import (
    YOUTUBE_MUSIC_AUTH_SCHEME,
    YouTubeMusicAuthService,
    YouTubeMusicOAuthError,
    oauth_token_needs_refresh,
)
from ops.config import Settings, get_settings
from ops.configuration import load_app_settings, load_saved_settings, save_app_settings
from ops.db import get_db
from ops.models import (
    LocalAdministrator,
    ProviderAccount,
    ProviderSearchCache,
    ProviderTrackMapping,
    SyncAction,
    SyncBaseline,
    SyncPair,
    SyncRun,
)
from ops.providers.base import ProviderError
from ops.providers.factory import create_provider
from ops.providers.youtube_music import YTMusicApiProvider
from ops.security.bootstrap import (
    BootstrapAuthorizationError,
    consume_bootstrap_token,
    verify_bootstrap_token,
)
from ops.security.crypto import CredentialCipher, CredentialEncryptionError
from ops.security.csrf import csrf_context, require_csrf
from ops.security.local_auth import (
    PasswordPolicyError,
    PasswordVerificationBusy,
    administrator,
    change_password,
    create_administrator,
    password_verification_slot,
    record_success,
    reserve_login_attempt,
    source_key,
    verify_password,
)
from ops.security.network import client_address
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncBaselineRepository,
    SyncPairRepository,
    SyncRunRepository,
)
from ops.sync.automatic import automation_binding
from ops.sync.coordinator import (
    AmbiguousSpotifyRemoval,
    PartialSyncRecoveryRequired,
    ReviewExpired,
    ReviewNotApplicable,
    SyncCoordinator,
    TrackMappingConflict,
)
from ops.sync.domain import InitialSyncPolicy, Side, SyncMode
from ops.sync.executor import PlanExecutionError
from ops.sync.leases import PairOperationBusy, acquire_pair_lease
from ops.sync.safety import Approval, DestructiveActionApprovalError, plan_fingerprint
from ops.sync.serialization import decode_baseline, decode_plan

router = APIRouter()
logger = logging.getLogger(__name__)
SUPPORTED_PROVIDERS = frozenset({"spotify", "youtube_music"})
NEW_PLAYLIST_PREFIX = "new:"
templates = Jinja2Templates(
    directory=os.environ.get(
        "OPS_TEMPLATES_DIR", str(Path(__file__).resolve().parents[3] / "templates")
    )
)
templates.context_processors.append(csrf_context)


def settings(session: Annotated[Session, Depends(get_db)]) -> Settings:
    return load_app_settings(session)


def _authenticated_session(request: Request, record: LocalAdministrator) -> None:
    request.session.clear()
    request.session["local_admin_authenticated"] = True
    request.session["local_admin_session_generation"] = record.session_generation


@router.get(
    "/auth/setup", response_class=HTMLResponse, response_model=None, include_in_schema=False
)
def local_administrator_setup(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> HTMLResponse | RedirectResponse:
    """Show the one-time local administrator password setup page."""

    if administrator(session) is not None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="local_auth_setup.html")


@router.post(
    "/auth/setup", response_class=HTMLResponse, response_model=None, include_in_schema=False
)
def save_local_administrator(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    bootstrap_code: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirmation: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse | RedirectResponse:
    if administrator(session) is not None:
        return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        verify_bootstrap_token(app_settings, bootstrap_code)
    except BootstrapAuthorizationError as exc:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_setup.html",
            context={"error": str(exc)},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if password != password_confirmation:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_setup.html",
            context={"error": "The passwords do not match."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        record = create_administrator(session, password)
    except (PasswordPolicyError, ValueError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_setup.html",
            context={"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    consume_bootstrap_token(app_settings)
    _authenticated_session(request, record)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/auth/login", response_class=HTMLResponse, response_model=None, include_in_schema=False
)
def local_administrator_login(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> HTMLResponse | RedirectResponse:
    record = administrator(session)
    if record is None:
        return RedirectResponse("/auth/setup", status_code=status.HTTP_303_SEE_OTHER)
    if (
        request.session.get("local_admin_authenticated")
        and request.session.get("local_admin_session_generation") == record.session_generation
    ):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="local_auth_login.html")


@router.post(
    "/auth/login", response_class=HTMLResponse, response_model=None, include_in_schema=False
)
def authenticate_local_administrator(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    password: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse | RedirectResponse:
    record = administrator(session)
    if record is None:
        return RedirectResponse("/auth/setup", status_code=status.HTTP_303_SEE_OTHER)
    key = source_key(client_address(request, app_settings))
    reservation = reserve_login_attempt(session, key)
    if not reservation.allowed:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_login.html",
            context={"error": "Too many sign-in attempts. Please wait and try again."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(reservation.retry_after_seconds)},
        )
    try:
        with password_verification_slot():
            verified = verify_password(password, record.password_hash)
    except PasswordVerificationBusy:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_login.html",
            context={"error": "Sign-in is busy. Please wait a moment and try again."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "1"},
        )
    if not verified:
        return templates.TemplateResponse(
            request=request,
            name="local_auth_login.html",
            context={"error": "Incorrect password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    record_success(session, record, key, password=password)
    _authenticated_session(request, record)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/auth/logout", response_class=RedirectResponse, include_in_schema=False)
def logout_local_administrator(
    request: Request, _: Annotated[None, Depends(require_csrf)] = None
) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def provider_for_account(session: Session, app_settings: Settings, account: ProviderAccount) -> Any:
    """Build a provider for UI discovery without exposing credential payloads."""

    if account.provider_name not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider account: {account.provider_name}")
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
        credentials = _refresh_youtube_music_credentials(
            session, app_settings, account, credentials
        )
        if not app_settings.ytmusic_client_id or not app_settings.ytmusic_client_secret:
            raise ValueError("YouTube Music OAuth settings are incomplete")
        credentials = {
            **credentials,
            "_ytmusic_client_id": app_settings.ytmusic_client_id,
            "_ytmusic_client_secret": app_settings.ytmusic_client_secret,
        }
    return create_provider(account, credentials)


def _refresh_youtube_music_credentials(
    session: Session,
    app_settings: Settings,
    account: ProviderAccount,
    credentials: dict[str, Any],
) -> dict[str, Any]:
    """Refresh OPS's encrypted Google token before YouTube API operations."""

    if credentials.get("auth_scheme") != YOUTUBE_MUSIC_AUTH_SCHEME:
        raise ValueError("YouTube Music needs to be reconnected for the current OAuth integration")
    if not oauth_token_needs_refresh(credentials):
        return credentials
    refresh_token = credentials.get("refresh_token")
    if (
        not refresh_token
        or not app_settings.ytmusic_client_id
        or not app_settings.ytmusic_client_secret
    ):
        raise ValueError("YouTube Music authorization expired; reconnect the account")
    refreshed = YouTubeMusicAuthService(
        app_settings.ytmusic_client_id, app_settings.ytmusic_client_secret
    ).refresh_token(str(refresh_token))
    merged = {
        **credentials,
        **refreshed,
        "refresh_token": refreshed.get("refresh_token", refresh_token),
    }
    ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key or "")
    ).save_credentials(account, merged)
    session.commit()
    return merged


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, app_settings: Annotated[Settings, Depends(settings)]) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": app_settings.app_name, "environment": app_settings.environment},
    )


@router.get("/setup", response_class=RedirectResponse, include_in_schema=False)
def setup_page() -> RedirectResponse:
    """Keep the old setup URL pointing at the merged settings screen."""

    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/setup/spotify", response_class=HTMLResponse, include_in_schema=False)
def spotify_setup_guide(
    request: Request, app_settings: Annotated[Settings, Depends(settings)]
) -> HTMLResponse:
    """Show detailed first-time Spotify developer setup instructions."""

    return templates.TemplateResponse(
        request=request,
        name="setup_spotify.html",
        context={"redirect_uri": app_settings.spotify_redirect_uri},
    )


@router.get("/setup/youtube_music", response_class=HTMLResponse, include_in_schema=False)
def youtube_music_setup_guide(request: Request) -> HTMLResponse:
    """Show detailed first-time Google Cloud and YouTube Music setup instructions."""

    return templates.TemplateResponse(request=request, name="setup_youtube_music.html")


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def about_page(request: Request) -> HTMLResponse:
    """Explain OPS's purpose, design motivation, and license."""

    return templates.TemplateResponse(request=request, name="about.html")


def automation_choices(session: Session, app_settings: Settings) -> list[dict[str, Any]]:
    choices = []
    for pair in SyncPairRepository(session).all():
        baseline = SyncBaselineRepository(session).latest_for_pair(pair.id)
        name = f"Pair {pair.id} (first sync required)"
        if baseline is not None:
            snapshot = decode_baseline(baseline.snapshot_json)
            arrow = "→" if pair.sync_mode == SyncMode.SOURCE_TO_TARGET.value else "↔"
            name = f"{snapshot.source.name} {arrow} {snapshot.target.name} (pair {pair.id})"
        choices.append(
            {
                "id": pair.id,
                "name": name,
                "active": automation_binding(pair) in app_settings.automatic_sync_bindings,
                "direction": "Source controls target"
                if pair.sync_mode == SyncMode.SOURCE_TO_TARGET.value
                else "Two-way",
            }
        )
    return choices


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    saved: str | None = None,
    restart_required: str | None = None,
) -> HTMLResponse:
    """Render the operator configuration screen without revealing secrets."""

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "saved": saved == "1",
            "restart_required": restart_required == "1",
            "spotify_client_id": app_settings.spotify_client_id or "",
            "spotify_redirect_uri": app_settings.spotify_redirect_uri,
            "spotify_secret_saved": bool(app_settings.spotify_client_secret),
            "ytmusic_client_id": app_settings.ytmusic_client_id or "",
            "ytmusic_secret_saved": bool(app_settings.ytmusic_client_secret),
            "scheduler_enabled": app_settings.scheduler_enabled,
            "automatic_pairs": automation_choices(session, app_settings),
            "sync_interval_minutes": app_settings.sync_interval_minutes,
            "explicit_preference": app_settings.explicit_preference,
            "https_mode_enabled": app_settings.https_mode_enabled,
            "https_mode_locked": get_settings().session_cookie_secure is not None,
        },
    )


@router.post("/settings/automation", response_class=RedirectResponse, include_in_schema=False)
def save_automation_route(
    session: Annotated[Session, Depends(get_db)],
    pair_ids: Annotated[list[int] | None, Form()] = None,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Store explicit, identity-bound consent without changing provider credentials."""
    pair_ids = pair_ids or []
    base_settings = get_settings()
    pairs = SyncPairRepository(session).all()
    if set(pair_ids) - {p.id for p in pairs}:
        raise HTTPException(status_code=400, detail="unknown playlist pair")
    save_app_settings(
        session,
        {
            **load_saved_settings(session, base_settings),
            "automatic_sync_bindings": [automation_binding(p) for p in pairs if p.id in pair_ids],
        },
        base_settings,
    )
    session.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings", response_class=RedirectResponse, include_in_schema=False)
def save_settings_route(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    spotify_client_id: Annotated[str, Form()] = "",
    spotify_client_secret: Annotated[str, Form()] = "",  # nosec B107
    spotify_redirect_uri: Annotated[str, Form()] = "",
    clear_spotify_secret: Annotated[str | None, Form()] = None,
    ytmusic_client_id: Annotated[str, Form()] = "",
    ytmusic_client_secret: Annotated[str, Form()] = "",  # nosec B107
    clear_ytmusic_secret: Annotated[str | None, Form()] = None,
    scheduler_enabled: Annotated[str | None, Form()] = None,
    https_mode_enabled: Annotated[str | None, Form()] = None,
    sync_interval_minutes: Annotated[int, Form()] = 60,
    explicit_preference: Annotated[
        Literal["no_preference", "prefer_explicit", "prefer_clean"], Form()
    ] = "no_preference",
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Encrypt and save provider and scheduler settings submitted by the UI."""

    base_settings = get_settings()
    saved = load_saved_settings(session, base_settings)
    current_settings = load_app_settings(session, base_settings)

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
        "automatic_sync_bindings": current_settings.automatic_sync_bindings,
        "sync_interval_minutes": max(1, min(sync_interval_minutes, 1440)),
        "explicit_preference": explicit_preference,
    }
    restart_required = False
    if base_settings.session_cookie_secure is None:
        requested_https_mode = https_mode_enabled is not None
        values["session_cookie_secure"] = requested_https_mode
        restart_required = requested_https_mode != current_settings.https_mode_enabled
    elif "session_cookie_secure" in saved:
        values["session_cookie_secure"] = bool(saved["session_cookie_secure"])
    save_app_settings(session, values, base_settings)
    session.commit()

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.reconfigure(load_app_settings(session, base_settings))
    target = "/settings?saved=1"
    if restart_required:
        target += "&restart_required=1"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/settings/password", response_class=HTMLResponse, response_model=None, include_in_schema=False
)
def change_local_administrator_password(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    password_confirmation: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse | RedirectResponse:
    """Change the local password after proving knowledge of the current password."""

    record = session.get(LocalAdministrator, 1)
    error: str | None = None
    try:
        with password_verification_slot():
            current_password_valid = bool(
                record and verify_password(current_password, record.password_hash)
            )
    except PasswordVerificationBusy:
        current_password_valid = False
        error = "Password verification is busy. Please wait a moment and try again."
    if error is None and not current_password_valid:
        error = "Current password is incorrect."
    if error is None and new_password != password_confirmation:
        error = "The new passwords do not match."
    if error is None and record is None:
        error = "The local administrator account is unavailable."
    if error is None and record is not None:
        try:
            change_password(session, record, new_password)
        except PasswordPolicyError as exc:
            error = str(exc)
    if error:
        app_settings = load_app_settings(session)
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "password_error": error,
                "spotify_client_id": app_settings.spotify_client_id or "",
                "spotify_redirect_uri": app_settings.spotify_redirect_uri,
                "spotify_secret_saved": bool(app_settings.spotify_client_secret),
                "ytmusic_client_id": app_settings.ytmusic_client_id or "",
                "ytmusic_secret_saved": bool(app_settings.ytmusic_client_secret),
                "scheduler_enabled": app_settings.scheduler_enabled,
                "automatic_pairs": automation_choices(session, app_settings),
                "sync_interval_minutes": app_settings.sync_interval_minutes,
                "explicit_preference": app_settings.explicit_preference,
                "https_mode_enabled": app_settings.https_mode_enabled,
                "https_mode_locked": get_settings().session_cookie_secure is not None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    _authenticated_session(request, record)
    return RedirectResponse("/settings?password_changed=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/healthz", tags=["system"])
def healthz() -> dict[str, str]:
    """Return a cheap process health response without requiring provider access."""

    return {"status": "ok", "service": "open-playlist-sync"}


@router.post("/auth/spotify/start", include_in_schema=False)
def spotify_start(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
    _: Annotated[None, Depends(require_csrf)] = None,
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
    code_verifier, code_challenge = service.pkce_pair()
    authorization_url, state = service.authorization_url(code_challenge=code_challenge)
    request.session["spotify_oauth_state"] = state
    request.session["spotify_pkce_verifier"] = code_verifier
    return RedirectResponse(authorization_url, status_code=status.HTTP_303_SEE_OTHER)


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
    code_verifier = request.session.pop("spotify_pkce_verifier", None)
    if (
        not code
        or not state
        or not expected_state
        or not code_verifier
        or not secrets.compare_digest(state, expected_state)
    ):
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
    token = service.exchange_code(code, code_verifier=code_verifier)
    token["expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=int(token.get("expires_in", 3600)))
    ).isoformat()
    profile = service.current_user(token["access_token"])
    account_repo = ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key)
    )
    connected_other = session.scalar(
        select(ProviderAccount).where(
            ProviderAccount.provider_name == "spotify",
            ProviderAccount.credentials_ciphertext.is_not(None),
            ProviderAccount.external_account_id != profile["id"],
        )
    )
    if connected_other is not None:
        return RedirectResponse(
            "/pairs?connection_error=spotify_account_change",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    account = account_repo.get_by_external_id("spotify", profile["id"])
    if account is None:
        account = ProviderAccount(provider_name="spotify", external_account_id=profile["id"])
    account.display_name = str(profile.get("display_name") or profile["id"])
    account_repo.save_credentials(account, token)
    session.commit()
    return RedirectResponse("/pairs?connected=spotify", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/auth/youtube_music/start", response_class=HTMLResponse, include_in_schema=False)
def youtube_music_start(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
    _: Annotated[None, Depends(require_csrf)] = None,
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


@router.post("/auth/youtube_music/complete", include_in_schema=False)
def youtube_music_complete(
    request: Request,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
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
    try:
        token = service.exchange_device_code(device_code)
    except YouTubeMusicOAuthError:
        return RedirectResponse(
            "/pairs?connection_error=youtube_music", status_code=status.HTTP_303_SEE_OTHER
        )
    if not token.get("access_token"):
        return RedirectResponse(
            "/pairs?connection_error=youtube_music", status_code=status.HTTP_303_SEE_OTHER
        )
    identity = YTMusicApiProvider(
        token,
        client_id=app_settings.ytmusic_client_id,
        client_secret=app_settings.ytmusic_client_secret,
    )
    try:
        external_account_id, display_name = identity.account_identity()
    except ProviderError as exc:
        cause = exc.__cause__
        logger.warning(
            "YouTube Music account identity check failed (provider=%s, cause=%s, status=%s)",
            type(exc).__name__,
            type(cause).__name__ if cause is not None else "none",
            YTMusicApiProvider._status_code(cause) if cause is not None else None,
        )
        return RedirectResponse(
            "/pairs?connection_error=youtube_music_identity",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    account_repo = ProviderAccountRepository(
        session, CredentialCipher(app_settings.credential_encryption_key)
    )
    all_youtube_accounts = list(
        session.scalars(
            select(ProviderAccount).where(
                ProviderAccount.provider_name == "youtube_music",
            )
        )
    )
    connected_accounts = [
        candidate
        for candidate in all_youtube_accounts
        if candidate.credentials_ciphertext is not None
    ]
    current_accounts = [
        candidate
        for candidate in connected_accounts
        if account_repo.load_credentials(candidate).get("auth_scheme") == YOUTUBE_MUSIC_AUTH_SCHEME
    ]
    connected_other = next(
        (
            candidate
            for candidate in current_accounts
            if candidate.external_account_id != external_account_id
        ),
        None,
    )
    if connected_other is not None:
        return RedirectResponse(
            "/pairs?connection_error=youtube_music_account_change",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    account = account_repo.get_by_external_id("youtube_music", external_account_id)
    if account is None:
        account = account_repo.get_by_external_id("youtube_music", "default")
    legacy_accounts = [
        candidate
        for candidate in connected_accounts
        if account_repo.load_credentials(candidate).get("auth_scheme") != YOUTUBE_MUSIC_AUTH_SCHEME
    ]
    if legacy_accounts and account is not None:
        # A newly connected account can coexist with stale records
        # only after an interrupted prior upgrade. Those pairs must remain
        # paused because the legacy credential format cannot safely run.
        session.execute(
            update(SyncPair)
            .where(
                (SyncPair.source_account_id.in_([candidate.id for candidate in legacy_accounts]))
                | (SyncPair.target_account_id.in_([candidate.id for candidate in legacy_accounts]))
            )
            .values(enabled=False)
        )
    if account is None and len(legacy_accounts) == 1:
        # An experimental adapter exposed a channel handle while this adapter
        # uses the stable Data API channel ID. Preserve the encrypted account record and pair IDs,
        # but pause every pair until the operator reviews the new provider view.
        account = legacy_accounts[0]
        account.external_account_id = external_account_id
        session.execute(
            update(SyncPair)
            .where(
                (SyncPair.source_account_id == account.id)
                | (SyncPair.target_account_id == account.id)
            )
            .values(enabled=False)
        )
    elif account is None and len(legacy_accounts) > 1:
        return RedirectResponse(
            "/pairs?connection_error=youtube_music_account_change",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if account is None and not connected_accounts:
        disconnected_accounts = [
            candidate
            for candidate in all_youtube_accounts
            if candidate.credentials_ciphertext is None
        ]
        if len(disconnected_accounts) == 1:
            # A user may have deliberately disconnected the legacy adapter
            # before upgrading. Reuse its pair references only when there is
            # exactly one unambiguous disconnected YouTube account.
            account = disconnected_accounts[0]
            account.external_account_id = external_account_id
            session.execute(
                update(SyncPair)
                .where(
                    (SyncPair.source_account_id == account.id)
                    | (SyncPair.target_account_id == account.id)
                )
                .values(enabled=False)
            )
    if account is None:
        account = ProviderAccount(
            provider_name="youtube_music",
            external_account_id=external_account_id,
            display_name=display_name,
        )
    elif account.external_account_id == "default":
        account.external_account_id = external_account_id
        session.execute(
            update(SyncPair)
            .where(
                (SyncPair.source_account_id == account.id)
                | (SyncPair.target_account_id == account.id)
            )
            .values(enabled=False)
        )
    account.display_name = display_name
    account_repo.save_credentials(account, token)
    session.commit()
    return RedirectResponse("/pairs?connected=youtube_music", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/runs", response_class=HTMLResponse, include_in_schema=False)
def recent_runs(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    def provider_label(provider_name: str) -> str:
        return {"spotify": "Spotify", "youtube_music": "YouTube Music"}.get(
            provider_name, provider_name.replace("_", " ").title()
        )

    runs = SyncRunRepository(session).recent(limit=100)
    pairs = {pair.id: pair for pair in SyncPairRepository(session).all()}
    accounts = {account.id: account for account in session.scalars(select(ProviderAccount))}
    activity: list[dict[str, Any]] = []
    for run in runs:
        events: list[dict[str, str]] = []
        if run.plan_json:
            try:
                plan = decode_plan(run.plan_json)
            except (TypeError, ValueError, KeyError):
                plan = None
            if run.status == "planned" and (
                plan is None or (not plan.actions and not plan.conflicts)
            ):
                # Scheduled no-change previews are useful operationally but
                # would otherwise hide the actual playlist history.
                continue
            pair = pairs.get(run.pair_id)
            statuses = {
                action.ordinal: action.status
                for action in session.scalars(select(SyncAction).where(SyncAction.run_id == run.id))
            }
            if plan is not None:
                for ordinal, action in enumerate(plan.actions):
                    destination = "the paired playlist"
                    if pair is not None:
                        account_id = (
                            pair.source_account_id
                            if action.side.value == "source"
                            else pair.target_account_id
                        )
                        account = accounts.get(account_id)
                        if account is not None:
                            destination = provider_label(account.provider_name)
                    events.append(
                        {
                            "operation": "Added to"
                            if action.action.value == "add_track"
                            else "Removed from",
                            "destination": destination,
                            "track": action.track.title,
                            "artists": ", ".join(action.track.artists),
                            "status": statuses.get(ordinal, "planned"),
                        }
                    )
        activity.append({"run": run, "events": events})
        if len(activity) == 25:
            break
    return templates.TemplateResponse(
        request=request, name="runs.html", context={"activity": activity}
    )


@router.get("/pairs", response_class=HTMLResponse, include_in_schema=False)
def pairs(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    connected: str | None = None,
    connection_error: str | None = None,
    playlist_created: str | None = None,
    source_selection: str | None = None,
    target_selection: str | None = None,
    initial_sync_policy: str | None = None,
    sync_mode: str | None = None,
) -> HTMLResponse:
    app_settings = load_app_settings(session)
    saved_accounts = list(
        session.scalars(
            select(ProviderAccount)
            .where(ProviderAccount.provider_name.in_(SUPPORTED_PROVIDERS))
            .order_by(ProviderAccount.id)
        )
    )
    accounts: list[ProviderAccount] = []
    provider_errors: dict[int, str] = {}
    cipher = CredentialCipher(app_settings.credential_encryption_key or "")
    account_repo = ProviderAccountRepository(session, cipher)
    for account in saved_accounts:
        try:
            credentials = account_repo.load_credentials(account)
        except CredentialEncryptionError:
            provider_errors[account.id] = (
                "Saved authorization could not be read; reconnect the account"
            )
            continue
        if not credentials.get("access_token"):
            provider_errors[account.id] = "Authorization did not complete; reconnect the account"
            continue
        accounts.append(account)
    provider_status = {
        "spotify": {
            "configured": all(
                (
                    app_settings.session_secret,
                    app_settings.credential_encryption_key,
                    app_settings.spotify_client_id,
                    app_settings.spotify_client_secret,
                )
            ),
            "connected": any(account.provider_name == "spotify" for account in accounts),
        },
        "youtube_music": {
            "configured": all(
                (
                    app_settings.session_secret,
                    app_settings.credential_encryption_key,
                    app_settings.ytmusic_client_id,
                    app_settings.ytmusic_client_secret,
                )
            ),
            "connected": any(account.provider_name == "youtube_music" for account in accounts),
        },
    }
    connection_message = {
        "spotify": "Spotify is connected. Choose playlists below.",
        "youtube_music": "YouTube Music is connected. Choose playlists below.",
    }.get(connected)
    if connection_error:
        connection_message = {
            "youtube_music_identity": "Google authorized OPS, but its YouTube channel could not "
            "be read. Check that YouTube Data API v3 is enabled and this account has a channel.",
            "youtube_music_account_change": "A different YouTube account is already connected. "
            "Disconnect it before changing accounts.",
        }.get(connection_error, "The connection was cancelled or rejected. Try again when ready.")
    if playlist_created:
        connection_message = "Playlist created. Choose both playlists, then create the pair."
    playlist_options = []
    for account in accounts:
        try:
            provider = provider_for_account(session, app_settings, account)
            playlists = provider.list_playlists()
        except (ProviderError, ValueError) as exc:
            playlists = ()
            provider_errors[account.id] = str(exc)
        playlist_options.append({"account": account, "playlists": playlists})
    account_by_id = {account.id: account for account in accounts}
    playlist_names = {
        (group["account"].id, playlist.provider_playlist_id): playlist.name
        for group in playlist_options
        for playlist in group["playlists"]
    }
    configured_pairs = []
    for pair in SyncPairRepository(session).all():
        source_account = account_by_id.get(pair.source_account_id)
        target_account = account_by_id.get(pair.target_account_id)
        if source_account is None or target_account is None:
            continue
        latest = SyncRunRepository(session).latest_for_pair(pair.id)
        pair_status = "Ready" if pair.enabled else "Paused"
        pair_detail = ""
        if pair.enabled and latest is not None:
            if latest.status == "authorization_required":
                pair_status = "Connection needs attention"
                pair_detail = (
                    "OPS retries access after one hour. If access remains blocked, "
                    "reconnect the service, then click Review to check immediately."
                )
            elif latest.status in {"failed", "partially_applied", "review_failed", "conflict"}:
                pair_status = "Review needed"
                pair_detail = "Open Activity or create a new review for details."
            elif latest.status in {"preparing", "applying"}:
                pair_status = "Working"
        automatic = automation_binding(pair) in app_settings.automatic_sync_bindings
        schedule_detail = (
            "Scheduled checks off"
            if not app_settings.scheduler_enabled
            else "Automatic sync enabled"
            if automatic
            else "Scheduled reviews only"
        )
        configured_pairs.append(
            {
                "id": pair.id,
                "enabled": pair.enabled,
                "source_name": playlist_names.get(
                    (source_account.id, pair.source_playlist_id), pair.source_playlist_id
                ),
                "source_provider": source_account.provider_name,
                "target_name": playlist_names.get(
                    (target_account.id, pair.target_playlist_id), pair.target_playlist_id
                ),
                "target_provider": target_account.provider_name,
                "sync_mode": pair.sync_mode,
                "status": pair_status,
                "status_detail": pair_detail,
                "schedule_detail": schedule_detail,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="pairs.html",
        context={
            "accounts": accounts,
            "pairs": configured_pairs,
            "playlist_options": playlist_options,
            "provider_status": provider_status,
            "connection_message": connection_message,
            "provider_errors": provider_errors,
            "selected_source": source_selection or "",
            "selected_target": target_selection or "",
            "selected_policy": initial_sync_policy or InitialSyncPolicy.MERGE.value,
            "selected_sync_mode": sync_mode or SyncMode.TWO_WAY.value,
        },
    )


def _existing_playlist_selection(selection: str) -> tuple[int, str]:
    try:
        account_raw, playlist_id = selection.split("|", 1)
        return int(account_raw), playlist_id
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid playlist selection") from exc


def _new_playlist_account(session: Session, selection: str) -> ProviderAccount:
    try:
        account_id = int(selection.removeprefix(NEW_PLAYLIST_PREFIX))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid new playlist selection") from exc
    account = session.get(ProviderAccount, account_id)
    if account is None or account.provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="provider account not found")
    return account


@router.post("/pairs", response_class=HTMLResponse, response_model=None, include_in_schema=False)
def create_pair(
    request: Request,
    source_selection: Annotated[str, Form()],
    target_selection: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
    initial_sync_policy: Annotated[str, Form()] = InitialSyncPolicy.MERGE.value,
    sync_mode: Annotated[str, Form()] = SyncMode.TWO_WAY.value,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse | RedirectResponse:
    try:
        policy = InitialSyncPolicy(initial_sync_policy)
        mode = SyncMode(sync_mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="invalid initial synchronization policy"
        ) from exc
    new_selections = [
        ("source", source_selection, target_selection),
        ("target", target_selection, source_selection),
    ]
    requested_new = [entry for entry in new_selections if entry[1].startswith(NEW_PLAYLIST_PREFIX)]
    if requested_new:
        if len(requested_new) != 1:
            raise HTTPException(status_code=400, detail="create one new playlist at a time")
        side, selection, other_selection = requested_new[0]
        account = _new_playlist_account(session, selection)
        other_account_id, _ = _existing_playlist_selection(other_selection)
        if account.id == other_account_id:
            raise HTTPException(
                status_code=400,
                detail="choose different services for the two sides of a pair",
            )
        return templates.TemplateResponse(
            request=request,
            name="create_playlist.html",
            context={
                "account": account,
                "side": side,
                "other_selection": other_selection,
                "initial_sync_policy": policy.value,
                "sync_mode": mode.value,
            },
        )

    source_account_id, source_playlist_id = _existing_playlist_selection(source_selection)
    target_account_id, target_playlist_id = _existing_playlist_selection(target_selection)
    if source_account_id == target_account_id:
        raise HTTPException(status_code=400, detail="source and target accounts must differ")
    source_account = session.get(ProviderAccount, source_account_id)
    target_account = session.get(ProviderAccount, target_account_id)
    if (
        source_account is None
        or target_account is None
        or source_account.provider_name not in SUPPORTED_PROVIDERS
        or target_account.provider_name not in SUPPORTED_PROVIDERS
    ):
        raise HTTPException(status_code=404, detail="provider account not found")
    session.add(
        SyncPair(
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            source_playlist_id=source_playlist_id,
            target_playlist_id=target_playlist_id,
            sync_mode=mode.value,
            initial_sync_policy=policy.value,
        )
    )
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/playlists", response_class=RedirectResponse, include_in_schema=False)
def create_playlist(
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    account_id: Annotated[int, Form()],
    side: Annotated[str, Form()],
    other_selection: Annotated[str, Form()],
    initial_sync_policy: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    sync_mode: Annotated[str, Form()] = SyncMode.TWO_WAY.value,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Create a private playlist on a connected provider, then return to pairing."""

    if side not in {"source", "target"}:
        raise HTTPException(status_code=400, detail="invalid playlist side")
    try:
        policy = InitialSyncPolicy(initial_sync_policy)
        mode = SyncMode(sync_mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="invalid initial synchronization policy"
        ) from exc
    playlist_name = name.strip()
    if not 1 <= len(playlist_name) <= 100:
        raise HTTPException(
            status_code=400, detail="playlist name must be between 1 and 100 characters"
        )
    account = session.get(ProviderAccount, account_id)
    if account is None or account.provider_name not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="provider account not found")
    other_account_id, _ = _existing_playlist_selection(other_selection)
    if account.id == other_account_id:
        raise HTTPException(status_code=400, detail="choose different services for the two sides")
    try:
        playlist = provider_for_account(session, app_settings, account).create_playlist(
            playlist_name, description.strip() or None
        )
    except (ProviderError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    created_selection = f"{account.id}|{playlist.provider_playlist_id}"
    selections = (
        {"source_selection": created_selection, "target_selection": other_selection}
        if side == "source"
        else {"source_selection": other_selection, "target_selection": created_selection}
    )
    selections.update(
        {
            "initial_sync_policy": policy.value,
            "sync_mode": mode.value,
            "playlist_created": "1",
        }
    )
    return RedirectResponse(
        f"/pairs?{urlencode(selections)}", status_code=status.HTTP_303_SEE_OTHER
    )


def matched_track_rows(groups: list[dict[str, Any]]) -> list[list[Any]]:
    """Align saved identities, retaining unmatched and duplicate occurrences."""
    from collections import defaultdict, deque

    if not groups:
        return []
    right = defaultdict(deque)
    for index, track in enumerate(groups[1]["tracks"] if len(groups) > 1 else ()):
        right[track.key].append((index, track))
    used = set()
    rows = []
    for track in groups[0]["tracks"]:
        match = right[track.key].popleft() if right[track.key] else None
        if match:
            used.add(match[0])
        rows.append([track, match[1] if match else None])
    if len(groups) > 1:
        rows.extend(
            [None, track] for index, track in enumerate(groups[1]["tracks"]) if index not in used
        )
    return rows


@router.get("/pairs/{pair_id}/matches", response_class=HTMLResponse, include_in_schema=False)
def saved_matches(
    request: Request, pair_id: int, session: Annotated[Session, Depends(get_db)]
) -> HTMLResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(404, "Pair not found")
    baseline = SyncBaselineRepository(session).latest_for_pair(pair.id)
    groups = []
    if baseline:
        state = decode_baseline(baseline.snapshot_json)
        for side, playlist, account_id in (
            ("source", state.source, pair.source_account_id),
            ("target", state.target, pair.target_account_id),
        ):
            account = session.get(ProviderAccount, account_id)
            groups.append(
                {
                    "side": side,
                    "provider": "Spotify"
                    if account.provider_name == "spotify"
                    else "YouTube Music",
                    "editable": not (side == "source" and pair.sync_mode == "source_to_target"),
                    "tracks": playlist.tracks,
                }
            )
    groups.sort(key=lambda group: group["provider"] != "Spotify")
    return templates.TemplateResponse(
        request=request,
        name="matches.html",
        context={"pair": pair, "groups": groups, "rows": matched_track_rows(groups)},
    )


@router.post(
    "/pairs/{pair_id}/matches",
    response_class=HTMLResponse,
    response_model=None,
    include_in_schema=False,
)
def change_saved_match(
    request: Request,
    pair_id: int,
    side: Annotated[str, Form()],
    track_id: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_db)],
    app_settings: Annotated[Settings, Depends(settings)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse | RedirectResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(404, "Pair not found")
    try:
        review = SyncCoordinator(session, app_settings, create_provider).prepare_replacement(
            pair, Side(side), track_id
        )
    except (ValueError, ProviderError, PlanExecutionError, PairOperationBusy) as exc:
        return templates.TemplateResponse(
            request=request,
            name="matches.html",
            context={"pair": pair, "groups": (), "error": str(exc)},
            status_code=400,
        )
    request.session[f"sync_review_token:{review.review_id}"] = review.approval_token
    return RedirectResponse(f"/sync/plan/{pair.id}?review_id={review.review_id}", status_code=303)


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


@router.post("/pairs/{pair_id}/mode", response_class=RedirectResponse, include_in_schema=False)
def set_pair_sync_mode(
    pair_id: int,
    session: Annotated[Session, Depends(get_db)],
    sync_mode: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Change propagation direction without changing either playlist."""

    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        mode = SyncMode(sync_mode).value
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid sync direction") from exc
    try:
        lease = acquire_pair_lease(session, pair.id)
    except PairOperationBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        if pair.sync_mode != mode:
            pair.sync_mode = mode
            session.execute(
                update(SyncRun)
                .where(
                    SyncRun.pair_id == pair.id,
                    SyncRun.status.in_(("planned", "conflict", "baseline_upgrade")),
                )
                .values(status="stale", approval_token_hash=None)
            )
            session.commit()
    finally:
        lease.release()
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
    session.execute(delete(ProviderTrackMapping).where(ProviderTrackMapping.pair_id == pair.id))
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
    accounts = list(
        session.scalars(
            select(ProviderAccount).where(ProviderAccount.provider_name == provider_name)
        )
    )
    if not accounts:
        raise HTTPException(status_code=404, detail="provider account not found")
    account_ids = [account.id for account in accounts]
    for account in accounts:
        account.credentials_ciphertext = None
        account.credential_key_id = None
    # Search evidence is not credential material, but it is account-specific
    # listening metadata and should not survive an explicit disconnect.
    session.execute(
        delete(ProviderSearchCache).where(ProviderSearchCache.account_id.in_(account_ids))
    )
    session.execute(
        update(SyncPair)
        .where(
            (SyncPair.source_account_id.in_(account_ids))
            | (SyncPair.target_account_id.in_(account_ids))
        )
        .values(enabled=False)
    )
    session.commit()
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/sync/plan/{pair_id}", response_class=HTMLResponse, include_in_schema=False)
def sync_plan(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    review_id: int | None = None,
) -> HTMLResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        review = SyncCoordinator(session, app_settings, create_provider).load_review(
            pair, review_id
        )
    except ReviewNotApplicable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    plan = review.plan if review else None
    token = request.session.get(f"sync_review_token:{review.review_id}", "") if review else ""
    return templates.TemplateResponse(
        request=request,
        name="sync_plan.html",
        context={
            "pair": pair,
            "plan": plan,
            "review": review,
            "fingerprint": plan_fingerprint(plan) if plan else "",
            "approval_token": token,
            "error": None if review else "Create a review to check both playlists.",
            "unresolved_tracks": review.unresolved_actions if review else (),
            "candidate_options": review.candidate_options if review else (),
        },
    )


@router.post("/sync/plan/{pair_id}", response_class=RedirectResponse, include_in_schema=False)
def create_sync_review(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> Response:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    try:
        review = SyncCoordinator(session, app_settings, create_provider).prepare_review(pair)
    except (
        ValueError,
        CredentialEncryptionError,
        ProviderError,
        PairOperationBusy,
        PartialSyncRecoveryRequired,
        TrackMappingConflict,
    ) as exc:
        return templates.TemplateResponse(
            request=request,
            name="sync_plan.html",
            context={"pair": pair, "plan": None, "review": None, "error": str(exc)},
            status_code=409
            if isinstance(exc, (TrackMappingConflict, PartialSyncRecoveryRequired))
            else 503,
        )
    for key in tuple(request.session):
        if str(key).startswith("sync_review_token:"):
            request.session.pop(key, None)
    request.session[f"sync_review_token:{review.review_id}"] = review.approval_token
    return RedirectResponse(
        f"/sync/plan/{pair_id}?review_id={review.review_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/sync/plan/{pair_id}/candidate", include_in_schema=False)
def select_sync_candidate(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    review_id: Annotated[int, Form()],
    action_index: Annotated[int, Form()],
    candidate_id: Annotated[str, Form()],
    _: Annotated[None, Depends(require_csrf)] = None,
) -> RedirectResponse:
    """Save an explicit close-match choice without performing a provider write."""

    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    if not request.session.get(f"sync_review_token:{review_id}"):
        raise HTTPException(status_code=403, detail="create a review in this browser session")
    try:
        SyncCoordinator(session, app_settings, create_provider).select_candidate(
            pair, review_id, action_index, candidate_id
        )
    except (ReviewExpired, ReviewNotApplicable, PairOperationBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        f"/sync/plan/{pair_id}?review_id={review_id}", status_code=status.HTTP_303_SEE_OTHER
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
    return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sync/apply/{pair_id}", response_class=HTMLResponse, include_in_schema=False)
def apply_sync_plan(
    request: Request,
    pair_id: int,
    app_settings: Annotated[Settings, Depends(settings)],
    session: Annotated[Session, Depends(get_db)],
    fingerprint: Annotated[str, Form()] = "",
    review_id: Annotated[int | None, Form()] = None,
    approval_token: Annotated[str, Form()] = "",  # nosec B107
    skip_unresolved: Annotated[str | None, Form()] = None,
    _: Annotated[None, Depends(require_csrf)] = None,
) -> HTMLResponse:
    pair = SyncPairRepository(session).get(pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="sync pair not found")
    review = None
    plan = None
    error = None
    try:
        coordinator = SyncCoordinator(session, app_settings, create_provider)
        if review_id is None:
            raise ReviewNotApplicable("create a fresh review before applying changes")
        review = coordinator.load_review(pair, review_id)
        if review is None:
            raise ReviewNotApplicable("the selected review is no longer available")
        plan = review.plan
        coordinator.apply(
            pair,
            plan,
            Approval(
                plan_fingerprint=fingerprint,
                confirmation="",
                review_id=review_id,
                token=approval_token,
            ),
            skip_unresolved=skip_unresolved is not None,
        )
    except (
        ValueError,
        CredentialEncryptionError,
        DestructiveActionApprovalError,
        ReviewExpired,
        ReviewNotApplicable,
        PairOperationBusy,
        AmbiguousSpotifyRemoval,
        TrackMappingConflict,
        PlanExecutionError,
        ProviderError,
    ) as exc:
        error = str(exc)
    if review_id is not None:
        request.session.pop(f"sync_review_token:{review_id}", None)
    if error is None:
        return RedirectResponse("/pairs", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="sync_plan.html",
        context={
            "pair": pair,
            "plan": plan,
            "review": review,
            "fingerprint": fingerprint,
            "approval_token": "",  # nosec B105
            "error": error,
            "unresolved_tracks": review.unresolved_actions if review else (),
            "candidate_options": review.candidate_options if review else (),
        },
    )
