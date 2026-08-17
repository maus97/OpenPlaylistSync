from urllib.parse import parse_qs, urlparse

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService


def test_spotify_authorization_url_contains_state_and_scopes() -> None:
    service = SpotifyOAuthService(
        SpotifyOAuthConfig("client-id", "client-secret", "http://localhost/callback")
    )

    url, state = service.authorization_url("state-value")
    query = parse_qs(urlparse(url).query)

    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert state == "state-value"
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["state-value"]
    assert "playlist-read-private" in query["scope"][0]
