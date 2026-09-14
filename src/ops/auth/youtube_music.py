"""Google device-flow boundary for YouTube Music.

Only normalized token dictionaries leave this module. OPS encrypts those
dictionaries in its existing provider-account record and never creates a
plaintext ``oauth.json`` file.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

import requests
from ytmusicapi.auth.oauth import OAuthCredentials
from ytmusicapi.exceptions import YTMusicError

from ops.providers.base import ProviderError, ProviderUnavailable
from ops.providers.errors import NetworkFailure, http_failure

YOUTUBE_MUSIC_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_MUSIC_AUTH_SCHEME = "ytmusicapi_oauth"
TOKEN_REFRESH_SKEW_SECONDS = 300


class YouTubeMusicOAuthError(ProviderError):
    """A sanitized device-flow failure safe to display to the administrator."""


class OAuthCredentialsClient(Protocol):
    """Small injectable OAuth surface used by OPS."""

    def get_code(self) -> Mapping[str, Any]: ...

    def token_from_code(self, device_code: str) -> Mapping[str, Any]: ...

    def refresh_token(self, refresh_token: str) -> Mapping[str, Any]: ...


def _safe_oauth_error(payload: object) -> YouTubeMusicOAuthError:
    error = payload.get("error") if isinstance(payload, Mapping) else None
    messages = {
        "authorization_pending": "Google authorization is not complete yet.",
        "slow_down": "Google asked OPS to wait before checking authorization again.",
        "access_denied": "Google authorization was denied.",
        "expired_token": "The Google setup code expired; start the connection again.",  # nosec B105
        "invalid_client": "Google rejected the configured OAuth client.",
        "unauthorized_client": "Google rejected the configured OAuth client.",
        "invalid_grant": "Google authorization expired; start the connection again.",
    }
    return YouTubeMusicOAuthError(
        messages.get(str(error), "Google could not complete authorization.")
    )


def normalize_oauth_token(
    payload: Mapping[str, Any],
    *,
    previous_refresh_token: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Return the refreshable token shape used by the provider adapters."""

    if payload.get("error") or not isinstance(payload.get("access_token"), str):
        raise _safe_oauth_error(payload)
    try:
        expires_in = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError) as exc:
        raise YouTubeMusicOAuthError("Google returned an invalid authorization response") from exc
    if not 1 <= expires_in <= 86_400:
        raise YouTubeMusicOAuthError("Google returned an invalid authorization response")
    refresh_token = payload.get("refresh_token") or previous_refresh_token
    if not isinstance(refresh_token, str) or not refresh_token:
        raise YouTubeMusicOAuthError("Google did not return a refreshable authorization")
    timestamp = int(time.time()) if now is None else now
    try:
        expires_at = int(payload.get("expires_at") or timestamp + expires_in)
    except (TypeError, ValueError) as exc:
        raise YouTubeMusicOAuthError("Google returned an invalid authorization response") from exc
    if expires_at <= timestamp - 86_400:
        raise YouTubeMusicOAuthError("Google returned an invalid authorization response")
    return {
        "auth_scheme": YOUTUBE_MUSIC_AUTH_SCHEME,
        "scope": str(payload.get("scope") or YOUTUBE_MUSIC_OAUTH_SCOPE),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "access_token": str(payload["access_token"]),
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "expires_at": expires_at,
    }


def oauth_token_needs_refresh(credentials: Mapping[str, Any], *, now: int | None = None) -> bool:
    """Treat legacy/non-refreshable payloads as needing an explicit reconnect."""

    if credentials.get("auth_scheme") != YOUTUBE_MUSIC_AUTH_SCHEME:
        return True
    try:
        expires_at = int(credentials["expires_at"])
    except (KeyError, TypeError, ValueError):
        return True
    timestamp = int(time.time()) if now is None else now
    return expires_at <= timestamp + TOKEN_REFRESH_SKEW_SECONDS


class OAuthSession(requests.Session):
    """Bound OAuth I/O and preserve refresh HTTP status before SDK parsing."""

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 20)
        response = super().request(method, url, **kwargs)
        data = kwargs.get("data")
        if isinstance(data, Mapping) and data.get("grant_type") == "refresh_token":
            response.raise_for_status()
        return response


class YouTubeMusicAuthService:
    """Use Google's device OAuth helper without exposing provider errors."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        credentials: OAuthCredentialsClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = credentials or OAuthCredentials(
            client_id, client_secret, session=OAuthSession()
        )

    @staticmethod
    def _call(operation) -> Mapping[str, Any]:  # type: ignore[no-untyped-def]
        try:
            payload = operation()
        except (requests.RequestException, YTMusicError, ValueError, TypeError) as exc:
            raise YouTubeMusicOAuthError("Google authorization could not be reached") from exc
        if not isinstance(payload, Mapping):
            raise YouTubeMusicOAuthError("Google returned an invalid authorization response")
        if payload.get("error"):
            raise _safe_oauth_error(payload)
        return payload

    def request_code(self) -> dict[str, Any]:
        payload = self._call(self.credentials.get_code)
        if not all(
            isinstance(payload.get(key), str) and payload[key]
            for key in ("device_code", "user_code", "verification_url")
        ):
            raise YouTubeMusicOAuthError("Google returned an invalid authorization response")
        return dict(payload)

    def exchange_device_code(self, device_code: str) -> dict[str, Any]:
        payload = self._call(lambda: self.credentials.token_from_code(device_code))
        return normalize_oauth_token(payload)

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        try:
            payload = self.credentials.refresh_token(refresh_token)
        except requests.RequestException as exc:
            response = exc.response
            if response is not None:
                try:
                    body = response.json()
                    reason = body.get("error") if isinstance(body, Mapping) else None
                except ValueError:
                    reason = None
                raise http_failure(
                    response.status_code,
                    reason=reason,
                    token=True,
                    retry=response.headers.get("Retry-After"),
                ) from exc
            raise NetworkFailure("Google renewal could not be reached") from exc
        except (YTMusicError, ValueError, TypeError) as exc:
            # SDK exceptions do not establish revocation: keep recovery automatic.
            raise ProviderUnavailable("Google renewal is temporarily unavailable") from exc
        if isinstance(payload, Mapping) and payload.get("error"):
            raise http_failure(400, reason=str(payload["error"]), token=True)
        if not isinstance(payload, Mapping):
            raise ProviderUnavailable("Google returned an incomplete renewal response")
        return normalize_oauth_token(payload, previous_refresh_token=refresh_token)
