from urllib.parse import parse_qs, urlparse

import pytest

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import (
    YOUTUBE_MUSIC_AUTH_SCHEME,
    YOUTUBE_MUSIC_OAUTH_SCOPE,
    YouTubeMusicAuthService,
    YouTubeMusicOAuthError,
    normalize_oauth_token,
    oauth_token_needs_refresh,
)


def test_spotify_authorization_url_contains_state_and_scopes() -> None:
    service = SpotifyOAuthService(
        SpotifyOAuthConfig("client-id", "client-secret", "http://localhost/callback")
    )

    verifier, challenge = service.pkce_pair()
    url, state = service.authorization_url("state-value", code_challenge=challenge)
    query = parse_qs(urlparse(url).query)

    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert state == "state-value"
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["state-value"]
    assert len(verifier) >= 43
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge]
    assert "user-read-private" in query["scope"][0]
    assert "playlist-read-private" in query["scope"][0]
    assert "playlist-read-collaborative" in query["scope"][0]
    assert "playlist-modify-private" in query["scope"][0]
    assert "playlist-modify-public" in query["scope"][0]


class FakeOAuthCredentials:
    def __init__(self) -> None:
        self.device_codes: list[str] = []
        self.refreshes: list[str] = []
        self.code_payload: dict[str, object] = {
            "device_code": "device-code",
            "user_code": "ABC-DEF",
            "verification_url": "https://www.google.com/device",
        }
        self.token_payload: dict[str, object] = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": YOUTUBE_MUSIC_OAUTH_SCOPE,
            "token_type": "Bearer",
        }

    def get_code(self) -> dict[str, object]:
        return self.code_payload

    def token_from_code(self, device_code: str) -> dict[str, object]:
        self.device_codes.append(device_code)
        return self.token_payload

    def refresh_token(self, refresh_token: str) -> dict[str, object]:
        self.refreshes.append(refresh_token)
        return {"access_token": "new-access-token", "expires_in": 1800}


def test_ytmusicapi_oauth_normalizes_a_refreshable_encrypted_token() -> None:
    fake = FakeOAuthCredentials()
    service = YouTubeMusicAuthService("client-id", "client-secret", credentials=fake)

    code = service.request_code()
    token = service.exchange_device_code("device-code")
    refreshed = service.refresh_token("refresh-token")

    assert code["verification_url"] == "https://www.google.com/device"
    assert fake.device_codes == ["device-code"]
    assert token["auth_scheme"] == YOUTUBE_MUSIC_AUTH_SCHEME
    assert token["scope"] == YOUTUBE_MUSIC_OAUTH_SCOPE
    assert isinstance(token["expires_at"], int)
    assert refreshed["refresh_token"] == "refresh-token"
    assert fake.refreshes == ["refresh-token"]


def test_ytmusicapi_oauth_sanitizes_provider_errors() -> None:
    fake = FakeOAuthCredentials()
    fake.token_payload = {
        "error": "invalid_client",
        "error_description": "sensitive provider detail",
    }
    service = YouTubeMusicAuthService("client-id", "client-secret", credentials=fake)

    with pytest.raises(YouTubeMusicOAuthError, match="rejected") as caught:
        service.exchange_device_code("device-code")

    assert "sensitive provider detail" not in str(caught.value)


def test_ytmusicapi_oauth_rejects_missing_device_fields_and_legacy_tokens() -> None:
    fake = FakeOAuthCredentials()
    fake.code_payload = {"device_code": "device-code"}
    with pytest.raises(YouTubeMusicOAuthError, match="invalid"):
        YouTubeMusicAuthService("client-id", "client-secret", credentials=fake).request_code()

    assert oauth_token_needs_refresh({"access_token": "legacy"}, now=1)
    current = normalize_oauth_token(
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}, now=100
    )
    assert not oauth_token_needs_refresh(current, now=101)
    assert oauth_token_needs_refresh(current, now=3_700)
