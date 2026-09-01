from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import (
    DEVICE_GRANT_TYPE,
    GOOGLE_DEVICE_CODE_URL,
    GOOGLE_TOKEN_URL,
    YOUTUBE_MUSIC_OAUTH_SCOPE,
    YouTubeMusicAuthService,
    YouTubeMusicOAuthError,
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


def test_youtube_music_oauth_scope_allows_library_read_and_write() -> None:
    assert YOUTUBE_MUSIC_OAUTH_SCOPE == "https://www.googleapis.com/auth/youtube.force-ssl"


def test_spotify_code_exchange_binds_pkce_verifier() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "access"})

    service = SpotifyOAuthService(
        SpotifyOAuthConfig("client-id", "client-secret", "https://ops.example/callback"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    token = service.exchange_code("authorization-code", code_verifier="pkce-verifier")

    assert token == {"access_token": "access"}
    body = parse_qs(requests[0].content.decode("ascii"))
    assert body["code_verifier"] == ["pkce-verifier"]
    assert body["code"] == ["authorization-code"]
    assert "authorization-code" not in str(requests[0].url)


def test_youtube_device_flow_uses_official_endpoints_and_sanitizes_errors() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == GOOGLE_DEVICE_CODE_URL:
            return httpx.Response(200, json={"device_code": "device", "user_code": "user"})
        return httpx.Response(
            400,
            json={"error": "invalid_client", "error_description": "sensitive provider detail"},
        )

    service = YouTubeMusicAuthService(
        "client-id",
        "client-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert service.request_code()["device_code"] == "device"
    with pytest.raises(YouTubeMusicOAuthError, match="rejected") as caught:
        service.exchange_device_code("device")
    assert "sensitive provider detail" not in str(caught.value)
    assert str(requests[1].url) == GOOGLE_TOKEN_URL
    body = parse_qs(requests[1].content.decode("ascii"))
    assert body["grant_type"] == [DEVICE_GRANT_TYPE]
