"""Spotify authorization-code flow without storing secrets in application code."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.common.security import generate_token

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)


@dataclass(frozen=True, slots=True)
class SpotifyOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


class SpotifyOAuthService:
    """Build and exchange Spotify OAuth authorization-code requests."""

    def __init__(self, config: SpotifyOAuthConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=20)

    def authorization_url(self, state: str | None = None) -> tuple[str, str]:
        state = state or generate_token(32)
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "scope": SPOTIFY_SCOPES,
                "state": state,
            }
        )
        return f"{SPOTIFY_AUTHORIZE_URL}?{query}", state

    def exchange_code(self, code: str) -> dict[str, Any]:
        response = self.client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
            },
            auth=(self.config.client_id, self.config.client_secret),
        )
        response.raise_for_status()
        return response.json()

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        response = self.client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(self.config.client_id, self.config.client_secret),
        )
        response.raise_for_status()
        return response.json()

    def current_user(self, access_token: str) -> dict[str, Any]:
        response = self.client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()
