"""Official Google limited-input-device OAuth for YouTube playlist access."""

from typing import Any

import httpx

YOUTUBE_MUSIC_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class YouTubeMusicOAuthError(RuntimeError):
    """A sanitized device-flow failure safe to display to the administrator."""


class YouTubeMusicAuthService:
    """Use Google's documented device and token endpoints with least privilege."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = client or httpx.Client(timeout=20)

    def _post(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise YouTubeMusicOAuthError("Google authorization could not be reached") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise YouTubeMusicOAuthError(
                "Google returned an invalid authorization response"
            ) from exc
        if response.is_error or payload.get("error"):
            error = str(payload.get("error") or "authorization_failed")
            messages = {
                "authorization_pending": "Google authorization is not complete yet.",
                "slow_down": "Google asked OPS to wait before checking authorization again.",
                "access_denied": "Google authorization was denied.",
                "expired_token": "The Google setup code expired; start the connection again.",  # nosec B105
                "invalid_client": "Google rejected the configured OAuth client.",
            }
            raise YouTubeMusicOAuthError(
                messages.get(error, "Google could not complete authorization.")
            )
        if not isinstance(payload, dict):
            raise YouTubeMusicOAuthError("Google returned an invalid authorization response")
        return payload

    def request_code(self) -> dict[str, Any]:
        return self._post(
            GOOGLE_DEVICE_CODE_URL,
            {"client_id": self.client_id, "scope": YOUTUBE_MUSIC_OAUTH_SCOPE},
        )

    def exchange_device_code(self, device_code: str) -> dict[str, Any]:
        return self._post(
            GOOGLE_TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
        )

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return self._post(
            GOOGLE_TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
