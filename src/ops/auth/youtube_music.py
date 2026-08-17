"""YouTube Music device-code authentication using ytmusicapi."""

from typing import Any

from ytmusicapi import OAuthCredentials

YOUTUBE_MUSIC_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"


class YouTubeMusicAuthService:
    """Wrap ytmusicapi's read/write OAuth flow for YouTube Music."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.credentials = OAuthCredentials(client_id, client_secret)

    def request_code(self) -> dict[str, Any]:
        """Request the device/user code shown to the operator."""

        return self.credentials.get_code()

    def exchange_device_code(self, device_code: str) -> dict[str, Any]:
        """Exchange the device code for refreshable OAuth credentials."""

        return self.credentials.token_from_code(device_code)

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an access token using the same OAuth client."""

        return self.credentials.refresh_token(refresh_token)
