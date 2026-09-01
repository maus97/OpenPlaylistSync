"""Spotify authorization-code flow without storing secrets in application code."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.common.security import generate_token

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"  # nosec B105
SPOTIFY_SCOPES = (
    "user-read-private playlist-read-private playlist-read-collaborative "
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

    @staticmethod
    def pkce_pair() -> tuple[str, str]:
        """Return a high-entropy verifier and its RFC 7636 S256 challenge."""

        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def authorization_url(
        self,
        state: str | None = None,
        *,
        code_challenge: str | None = None,
    ) -> tuple[str, str]:
        state = state or generate_token(32)
        parameters = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": state,
        }
        if code_challenge:
            parameters.update({"code_challenge_method": "S256", "code_challenge": code_challenge})
        query = urlencode(parameters)
        return f"{SPOTIFY_AUTHORIZE_URL}?{query}", state

    def exchange_code(self, code: str, *, code_verifier: str | None = None) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        response = self.client.post(
            SPOTIFY_TOKEN_URL,
            data=form,
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
