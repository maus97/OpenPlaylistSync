import httpx
import pytest

from ops.providers.base import AuthorizationRequired, RateLimited
from ops.providers.types import ProviderTrack
from ops.providers.youtube_music import YouTubeMusicProvider


def test_youtube_provider_maps_official_api_playlists_and_tracks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path == "/youtube/v3/playlists" and request.url.params.get("mine") == "true":
            return httpx.Response(
                200, json={"items": [{"id": "playlist-1", "snippet": {"title": "Mix"}}]}
            )
        if request.url.path == "/youtube/v3/playlists":
            return httpx.Response(
                200,
                json={"items": [{"id": "playlist-1", "snippet": {"title": "Mix"}}]},
            )
        if request.url.path == "/youtube/v3/playlistItems":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "playlist-item-1",
                            "snippet": {"position": 3},
                            "contentDetails": {"videoId": "video-1"},
                        }
                    ]
                },
            )
        if request.url.path == "/youtube/v3/videos":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "video-1",
                            "snippet": {"title": "Song", "channelTitle": "Artist"},
                            "contentDetails": {"duration": "PT3M"},
                        }
                    ]
                },
            )
        if request.url.path == "/youtube/v3/search":
            return httpx.Response(200, json={"items": [{"id": {"videoId": "video-1"}}]})
        return httpx.Response(404)

    provider = YouTubeMusicProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://www.googleapis.com/youtube/v3", transport=httpx.MockTransport(handler)
        ),
    )

    playlists = provider.list_playlists()
    snapshot = provider.get_playlist("youtube_music:playlist-1")
    resolved = provider.search_track(ProviderTrack("", "Song", ("Artist",)))

    assert playlists[0].provider_playlist_id == "youtube_music:playlist-1"
    assert snapshot.tracks[0].occurrence_id == "playlist-item-1"
    assert snapshot.tracks[0].duration_ms == 180_000
    assert resolved is not None
    assert resolved.provider_track_id == "youtube_music:video-1"


def test_youtube_provider_writes_through_official_api_and_removes_exact_item() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/youtube/v3/playlists":
            return httpx.Response(200, json={"id": "new-playlist", "snippet": {"title": "New"}})
        return httpx.Response(200, json={})

    provider = YouTubeMusicProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://www.googleapis.com/youtube/v3", transport=httpx.MockTransport(handler)
        ),
    )
    track = ProviderTrack(
        "youtube_music:video-1", "Song", ("Artist",), occurrence_id="playlist-item-1"
    )

    created = provider.create_playlist("New")
    provider.add_tracks("youtube_music:new-playlist", [track])
    provider.remove_tracks("youtube_music:new-playlist", [track])

    assert created.provider_playlist_id == "youtube_music:new-playlist"
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/youtube/v3/playlists"),
        ("POST", "/youtube/v3/playlistItems"),
        ("DELETE", "/youtube/v3/playlistItems"),
    ]
    assert requests[-1].url.params["id"] == "playlist-item-1"


def test_youtube_removal_rejects_item_without_playlist_item_identifier() -> None:
    provider = YouTubeMusicProvider(access_token="token")

    with pytest.raises(ValueError, match="playlist item"):
        provider.remove_tracks(
            "youtube_music:playlist-1",
            [ProviderTrack("youtube_music:video-1", "Song", ("Artist",))],
        )


def test_youtube_provider_requires_a_linked_account() -> None:
    with pytest.raises(AuthorizationRequired):
        YouTubeMusicProvider().list_playlists()


def test_youtube_provider_reports_http_429_as_rate_limited() -> None:
    provider = YouTubeMusicProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://www.googleapis.com/youtube/v3",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429,
                    json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                )
            ),
        ),
    )

    with pytest.raises(RateLimited):
        provider.search_track(ProviderTrack("spotify:track", "Song", ("Artist",)))


def test_youtube_search_accepts_official_video_title_without_false_lyrics_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/youtube/v3/search":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": "official"}},
                        {"id": {"videoId": "lyrics"}},
                    ]
                },
            )
        if request.url.path == "/youtube/v3/videos":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "official",
                            "snippet": {
                                "title": "Ariana Grande - 7 rings (Official Video)",
                                "channelTitle": "ArianaGrandeVevo",
                            },
                            "contentDetails": {"duration": "PT3M5S"},
                        },
                        {
                            "id": "lyrics",
                            "snippet": {
                                "title": "Ariana Grande - 7 rings (Lyrics)",
                                "channelTitle": "Lyrics Channel",
                            },
                            "contentDetails": {"duration": "PT2M59S"},
                        },
                    ]
                },
            )
        return httpx.Response(404)

    provider = YouTubeMusicProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://www.googleapis.com/youtube/v3", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack("spotify:track-1", "7 rings", ("Ariana Grande",), duration_ms=180_000)
    )

    assert resolved is not None
    assert resolved.provider_track_id == "youtube_music:official"


def test_youtube_search_normalizes_spotify_soundtrack_labels() -> None:
    requested = ProviderTrack(
        "spotify:open-door",
        'Love Is an Open Door - From "Frozen"/Soundtrack Version',
        ("Kristen Bell", "Santino Fontana"),
        duration_ms=124_733,
    )
    resolved = YouTubeMusicProvider._choose_search_candidate(
        requested,
        (
            ProviderTrack(
                "youtube_music:official",
                'Kristen Bell, Santino Fontana - Love Is an Open Door (From "Frozen"/Sing-Along)',
                ("DisneyMusicVEVO",),
                duration_ms=126_000,
            ),
        ),
    )

    assert resolved is not None
    assert resolved.provider_track_id == "youtube_music:official"
    assert YouTubeMusicProvider._requested_title_tokens("From the Start") == (
        "from",
        "the",
        "start",
    )


def test_youtube_search_prefers_standard_topic_upload_over_acoustic_variant() -> None:
    requested = ProviderTrack(
        "spotify:a-little-more", "A Little More", ("Ed Sheeran",), duration_ms=192_043
    )
    resolved = YouTubeMusicProvider._choose_search_candidate(
        requested,
        (
            ProviderTrack(
                "youtube_music:acoustic",
                "Ed Sheeran - A Little More (Official Acoustic Video)",
                ("Ed Sheeran",),
                duration_ms=201_000,
            ),
            ProviderTrack(
                "youtube_music:topic",
                "A Little More",
                ("Ed Sheeran - Topic",),
                duration_ms=193_000,
            ),
        ),
    )

    assert resolved is not None
    assert resolved.provider_track_id == "youtube_music:topic"
