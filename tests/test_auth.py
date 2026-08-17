from urllib.parse import parse_qs, urlparse

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YOUTUBE_MUSIC_OAUTH_SCOPE


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
    assert "playlist-read-collaborative" in query["scope"][0]
    assert "playlist-modify-private" in query["scope"][0]
    assert "playlist-modify-public" in query["scope"][0]


def test_youtube_music_oauth_scope_allows_library_read_and_write() -> None:
    assert YOUTUBE_MUSIC_OAUTH_SCOPE == "https://www.googleapis.com/auth/youtube"
