import httpx

from ops.providers.spotify import SpotifyProvider
from ops.providers.types import ProviderTrack


def test_spotify_provider_maps_read_only_playlist_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(200, json={"items": [{"id": "playlist-1", "name": "Mix"}]})
        if request.url.path == "/v1/playlists/playlist-1":
            return httpx.Response(
                200,
                json={
                    "id": "playlist-1",
                    "name": "Mix",
                    "tracks": {
                        "items": [
                            {
                                "track": {
                                    "id": "track-1",
                                    "name": "Song",
                                    "artists": [{"name": "Artist"}],
                                    "album": {"name": "Album"},
                                    "duration_ms": 1000,
                                    "external_ids": {"isrc": "US-AAA-00-00001"},
                                }
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "track-1",
                                "name": "Song",
                                "artists": [{"name": "Artist"}],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://api.spotify.com/v1",
        transport=httpx.MockTransport(handler),
    )
    provider = SpotifyProvider(access_token="token", client=client)

    playlists = provider.list_playlists()
    snapshot = provider.get_playlist("spotify:playlist-1")
    resolved = provider.search_track(ProviderTrack("", "Song", ("Artist",)))

    assert playlists[0].provider_playlist_id == "spotify:playlist-1"
    assert snapshot.tracks[0].isrc == "US-AAA-00-00001"
    assert resolved is not None
    assert resolved.provider_track_id == "spotify:track-1"


def test_spotify_provider_follows_playlist_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me/playlists" and request.url.params.get("offset") is None:
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "playlist-1", "name": "First"}],
                    "next": "https://api.spotify.com/v1/me/playlists?offset=50",
                },
            )
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(200, json={"items": [{"id": "playlist-2", "name": "Second"}]})
        return httpx.Response(404)

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    assert [playlist.name for playlist in provider.list_playlists()] == ["First", "Second"]
