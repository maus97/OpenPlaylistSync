"""Shared proactive refresh and cross-process refresh exclusion."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService, oauth_token_needs_refresh
from ops.models import ProviderAccount
from ops.providers.base import AuthorizationRequired, ProviderUnavailable
from ops.providers.errors import PermissionDenied
from ops.providers.health import verified
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import ProviderAccountRepository


def credentials_for(
    session,
    settings,
    account,
    *,
    rejected_token=None,
    spotify_service=SpotifyOAuthService,
    youtube_service=YouTubeMusicAuthService,
):
    started = datetime.now(UTC)
    try:
        return _credentials(
            session, settings, account, started, rejected_token, spotify_service, youtube_service
        )
    except Exception as exc:
        exc.provider = account.provider_name
        exc.account_id = account.id
        exc.operation = "refresh"
        exc.operation_started_at = started
        raise


def _credentials(
    session, settings, account, started, rejected_token, spotify_service, youtube_service
):
    cipher = CredentialCipher(settings.credential_encryption_key or "")
    repo = ProviderAccountRepository(session, cipher)
    session.refresh(account)
    original = account.credentials_ciphertext
    data = repo.load_credentials(account)
    if account.provider_name not in {"spotify", "youtube_music"}:
        return data  # synthetic providers never contact OAuth
    if not data.get("access_token") or not data.get("refresh_token"):
        raise AuthorizationRequired("Connect this service to authorize access.")
    if account.provider_name == "spotify":
        try:
            expiry = datetime.fromisoformat(str(data["expires_at"]))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            expired = expiry <= started + timedelta(minutes=5)
        except (KeyError, ValueError, TypeError):
            expired = True
    else:
        if data.get("auth_scheme") != "ytmusicapi_oauth":
            raise AuthorizationRequired("Reconnect YouTube Music for the current integration.")
        expired = oauth_token_needs_refresh(data)
    # A different worker/reconnect already replaced the rejected access token.
    refresh = expired or (rejected_token is not None and data["access_token"] == rejected_token)
    if refresh:
        lock = secrets.token_urlsafe(32)
        result = session.execute(
            update(ProviderAccount)
            .execution_options(synchronize_session="fetch")
            .where(
                ProviderAccount.id == account.id,
                ProviderAccount.credentials_ciphertext == original,
                or_(
                    ProviderAccount.refresh_lock_token.is_(None),
                    ProviderAccount.refresh_lock_until < started,
                ),
            )
            .values(refresh_lock_token=lock, refresh_lock_until=started + timedelta(minutes=2))
        )
        session.commit()
        if result.rowcount != 1:
            raise ProviderUnavailable("Connection renewal is already running. OPS will retry.")
        try:
            if account.provider_name == "spotify":
                if not settings.spotify_client_id or not settings.spotify_client_secret:
                    raise PermissionDenied("Spotify app configuration is incomplete.")
                service = spotify_service(
                    SpotifyOAuthConfig(
                        settings.spotify_client_id,
                        settings.spotify_client_secret,
                        settings.spotify_redirect_uri,
                    )
                )
            else:
                if not settings.ytmusic_client_id or not settings.ytmusic_client_secret:
                    raise PermissionDenied("YouTube Music app configuration is incomplete.")
                service = youtube_service(
                    settings.ytmusic_client_id, settings.ytmusic_client_secret
                )
            refreshed = service.refresh_token(data["refresh_token"])
            if not isinstance(refreshed.get("access_token"), str) or not refreshed["access_token"]:
                raise ProviderUnavailable("The service returned an incomplete renewal response.")
            merged = {
                **data,
                **refreshed,
                "refresh_token": refreshed.get("refresh_token") or data["refresh_token"],
            }
            if account.provider_name == "spotify":
                merged["expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
                ).isoformat()
            result = session.execute(
                update(ProviderAccount)
                .execution_options(synchronize_session="fetch")
                .where(
                    ProviderAccount.id == account.id,
                    ProviderAccount.credentials_ciphertext == original,
                    ProviderAccount.refresh_lock_token == lock,
                    ProviderAccount.refresh_lock_until > datetime.now(UTC),
                )
                .values(credentials_ciphertext=cipher.encrypt(merged))
            )
            if result.rowcount:
                verified(session, account.id, started, operation="refresh")
            session.commit()
            session.refresh(account)
            if not result.rowcount and account.credentials_ciphertext == original:
                raise ProviderUnavailable("Connection renewal lease expired. OPS will retry.")
            data = repo.load_credentials(account)
            if not data.get("access_token"):
                raise AuthorizationRequired("This service was disconnected during renewal.")
        finally:
            session.execute(
                update(ProviderAccount)
                .execution_options(synchronize_session="fetch")
                .where(
                    ProviderAccount.id == account.id,
                    ProviderAccount.refresh_lock_token == lock,
                )
                .values(refresh_lock_token=None, refresh_lock_until=None)
            )
            session.commit()
    if account.provider_name == "youtube_music":
        data = {
            **data,
            "_ytmusic_client_id": settings.ytmusic_client_id,
            "_ytmusic_client_secret": settings.ytmusic_client_secret,
        }
    return data


def enable_refresh(provider, session, settings, account, **services):
    """Retry a rejected HTTP request once, never a composite add/remove operation."""
    if account.provider_name in {"spotify", "youtube_music"} and hasattr(
        provider, "set_token_refresher"
    ):
        provider.set_token_refresher(
            lambda rejected: credentials_for(
                session, settings, account, rejected_token=rejected, **services
            )["access_token"]
        )
    return provider
