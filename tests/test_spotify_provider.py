from dataclasses import replace

import httpx
import pytest

from ops.providers.base import AuthorizationRequired, ProviderUnavailable
from ops.providers.spotify import SpotifyProvider
from ops.providers.types import ProviderTrack


def test_acoustic_exact_match_is_not_ambiguous_with_guitar_arrangement() -> None:
    requested = ProviderTrack(
        "youtube_music:mercy", "Mercy (Acoustic)", ("Shawn Mendes - Topic",), duration_ms=220000
    )
    acoustic = ProviderTrack(
        "spotify:acoustic",
        "Mercy - Acoustic",
        ("Shawn Mendes",),
        duration_ms=219160,
        isrc="USUM71606080",
    )
    guitar = ProviderTrack(
        "spotify:guitar",
        "Mercy - Acoustic Guitar",
        ("Shawn Mendes",),
        duration_ms=221200,
        isrc="USUM71700308",
    )
    assert SpotifyProvider.resolve_search_candidates(requested, (guitar, acoustic)) == acoustic
    guitar_requested = replace(requested, title="Mercy (Acoustic Guitar)")
    assert SpotifyProvider.resolve_search_candidates(guitar_requested, (acoustic, guitar)) == guitar
    assert SpotifyProvider.resolve_search_candidates(guitar_requested, (acoustic,)) is None


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
                },
            )
        if request.url.path == "/v1/playlists/playlist-1/items":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "item": {
                                "id": "track-1",
                                "name": "Song",
                                "type": "track",
                                "artists": [{"name": "Artist"}],
                                "album": {"name": "Album"},
                                "duration_ms": 1000,
                                "external_ids": {"isrc": "US-AAA-00-00001"},
                            }
                        }
                    ]
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


def test_spotify_provider_writes_through_current_playlist_items_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    track = ProviderTrack("spotify:track-1", "Song", ("Artist",), position=2)

    provider.add_tracks("spotify:playlist-1", [track])
    provider.remove_tracks("spotify:playlist-1", [track])

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/playlists/playlist-1/items"),
        ("DELETE", "/v1/playlists/playlist-1/items"),
    ]
    assert requests[0].content == b'{"uris":["spotify:track:track-1"]}'
    assert requests[1].content == b'{"items":[{"uri":"spotify:track:track-1"}]}'


def test_spotify_provider_creates_a_private_playlist_through_current_me_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "playlist-1", "name": "New playlist"})

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    playlist = provider.create_playlist("New playlist", "Created by OPS")

    assert playlist.provider_playlist_id == "spotify:playlist-1"
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/me/playlists")
    ]
    assert requests[0].content == (
        b'{"name":"New playlist","description":"Created by OPS","public":false}'
    )


def test_spotify_provider_explains_forbidden_access_as_a_reconnect_request() -> None:
    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1",
            transport=httpx.MockTransport(lambda _: httpx.Response(403)),
        ),
    )

    with pytest.raises(AuthorizationRequired, match="reconnect Spotify"):
        provider.list_playlists()


def test_spotify_provider_explains_playlist_specific_forbidden_access() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"id": "playlist-1", "name": "Shared"})
        return httpx.Response(403)

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(AuthorizationRequired, match="owner or a collaborator"):
        provider.get_playlist("spotify:playlist-1")


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


def test_spotify_provider_rejects_untrusted_pagination_url_before_sending_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [{"id": "playlist-1", "name": "First"}],
                "next": "https://attacker.invalid/collect",
            },
        )

    provider = SpotifyProvider(
        access_token="sensitive-token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    with pytest.raises(ProviderUnavailable, match="pagination URL"):
        provider.list_playlists()
    assert [request.url.host for request in requests] == ["api.spotify.com"]


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"items": None},
        {"items": [], "total": 1},
        {"items": [{"item": None}]},
        {"items": [{"item": {"name": "Unavailable recording"}}]},
        {"items": [], "offset": 50},
    ],
)
def test_spotify_refuses_incomplete_playlist_snapshot(page: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=page)
        return httpx.Response(200, json={"id": "playlist-1", "name": "Mix"})

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(ProviderUnavailable):
        provider.get_playlist("spotify:playlist-1")


def test_spotify_empty_playlist_is_valid_when_explicitly_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"items": [], "total": 0, "offset": 0})
        return httpx.Response(200, json={"id": "playlist-1"})

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    assert provider.get_playlist("spotify:playlist-1").tracks == ()


def test_spotify_repeated_page_stops_without_spending_more_requests() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 2:
            pytest.fail("repeated pagination should stop before a third request")
        return httpx.Response(
            200,
            json={
                "items": [{"id": "playlist-1"}],
                "next": "https://api.spotify.com/v1/me/playlists?offset=50",
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(ProviderUnavailable, match="repeated"):
        provider.list_playlists()
    assert calls == 2


def test_spotify_complete_pages_preserve_duplicate_occurrences_and_skip_local_files() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/items"):
            return httpx.Response(200, json={"id": "playlist-1"})
        if request.url.params.get("offset"):
            return httpx.Response(
                200,
                json={
                    "items": [{"item": {"id": "song", "name": "Song"}}],
                    "total": 3,
                    "offset": 2,
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {"item": {"id": "song", "name": "Song"}},
                    {"item": {"is_local": True, "id": None}},
                ],
                "total": 3,
                "offset": 0,
                "next": "https://api.spotify.com/v1/playlists/playlist-1/items?offset=2",
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    tracks = provider.get_playlist("spotify:playlist-1").tracks
    assert [track.provider_track_id for track in tracks] == ["spotify:song", "spotify:song"]
    assert [track.position for track in tracks] == [0, 2]


def test_spotify_provider_matches_topic_channel_metadata_from_youtube() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        assert request.url.params["q"] == "Back In Black AC/DC"
        assert request.url.params["limit"] == "10"
        assert "market" not in request.url.params
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "back-in-black",
                            "name": "Back In Black",
                            "artists": [{"name": "AC/DC"}],
                            "duration_ms": 255_000,
                        }
                    ]
                }
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack("youtube_music:back-in-black", "Back In Black", ("AC/DC - Topic",))
    )

    assert resolved is not None
    assert resolved.provider_track_id == "spotify:back-in-black"


def test_spotify_provider_strips_official_video_metadata_from_youtube() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        assert request.url.params["q"] == "Try P!nk"
        return httpx.Response(
            200,
            json={
                "tracks": {"items": [{"id": "try", "name": "Try", "artists": [{"name": "P!nk"}]}]}
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack("youtube_music:try", "P!nk - Try (Official Video)", ("PinkVEVO",))
    )

    assert resolved is not None
    assert resolved.provider_track_id == "spotify:try"


def test_spotify_provider_strips_artist_prefix_from_artist_operated_youtube_channel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/search"
        assert request.url.params["q"] == "Beautiful Things (Acoustic) Benson Boone"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "acoustic",
                            "name": "Beautiful Things - Acoustic",
                            "artists": [{"name": "Benson Boone"}],
                            "duration_ms": 201_248,
                        }
                    ]
                }
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack(
            "youtube_music:acoustic",
            "Benson Boone - Beautiful Things (Acoustic) [Official Audio]",
            ("Benson Boone",),
            duration_ms=202_000,
        )
    )

    assert resolved is not None
    assert resolved.provider_track_id == "spotify:acoustic"


def test_spotify_provider_collapses_same_isrc_catalogue_results_before_ambiguity_check() -> None:
    requested = ProviderTrack(
        "youtube_music:world", "A Whole New World", ("Lea Salonga",), duration_ms=160_800
    )
    standard = ProviderTrack(
        "spotify:standard",
        "A Whole New World",
        ("Lea Salonga", "Brad Kane", "Disney"),
        album="Aladdin Special Edition",
        duration_ms=160_800,
        isrc="USWD10423000",
    )
    soundtrack = ProviderTrack(
        "spotify:soundtrack",
        'A Whole New World - From "Aladdin" / Soundtrack Version',
        ("Lea Salonga", "Brad Kane", "Disney"),
        album="Disney Summer Songs",
        duration_ms=160_400,
        isrc="USWD10423000",
    )

    assert SpotifyProvider._choose_search_candidate(requested, (standard, soundtrack)) == standard


def test_spotify_provider_offers_distinct_ambiguous_recordings_once_for_manual_review() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "release-one",
                            "name": "A Little More",
                            "artists": [{"name": "Ed Sheeran"}],
                            "duration_ms": 192_043,
                            "external_ids": {"isrc": "GBAHS2500267"},
                        },
                        {
                            "id": "release-one-duplicate",
                            "name": "A Little More",
                            "artists": [{"name": "Ed Sheeran"}],
                            "duration_ms": 192_043,
                            "external_ids": {"isrc": "GBAHS2500267"},
                        },
                        {
                            "id": "release-two",
                            "name": "A Little More",
                            "artists": [{"name": "Ed Sheeran"}],
                            "duration_ms": 192_499,
                            "external_ids": {"isrc": "GBAHS2500862"},
                        },
                    ]
                }
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )
    requested = ProviderTrack(
        "youtube_music:a-little-more",
        "A Little More",
        ("Ed Sheeran - Topic",),
        duration_ms=193_000,
    )

    assert provider.search_track(requested) is None
    assert [
        candidate.provider_track_id for candidate in provider.close_track_candidates(requested)
    ] == [
        "spotify:release-two",
        "spotify:release-one-duplicate",
    ]
    assert len(requests) == 1


def test_spotify_provider_uses_exact_album_to_resolve_distinct_recordings() -> None:
    requested = ProviderTrack(
        "youtube_music:a-little-more",
        "A Little More",
        ("Ed Sheeran",),
        album="A Little More",
        duration_ms=193_000,
    )
    single = ProviderTrack(
        "spotify:single",
        "A Little More",
        ("Ed Sheeran",),
        album="A Little More",
        duration_ms=192_499,
        isrc="GBAHS2500862",
        explicit=False,
    )
    album = ProviderTrack(
        "spotify:album",
        "A Little More",
        ("Ed Sheeran",),
        album="Play",
        duration_ms=192_043,
        isrc="GBAHS2500267",
        explicit=True,
    )

    assert SpotifyProvider.resolve_search_candidates(requested, (album, single)) == single


def test_spotify_provider_uses_content_preference_only_for_unknown_rating_ties() -> None:
    requested = ProviderTrack(
        "youtube_music:peaches",
        "Peaches",
        ("Justin Bieber - Topic",),
        duration_ms=199_000,
    )
    explicit = ProviderTrack(
        "spotify:explicit",
        "Peaches (feat. Daniel Caesar & Giveon)",
        ("Justin Bieber", "Daniel Caesar", "GIVĒON"),
        album="Justice",
        duration_ms=198_081,
        isrc="USUM72102636",
        explicit=True,
    )
    clean = ProviderTrack(
        "spotify:clean",
        "Peaches (feat. Daniel Caesar & Giveon)",
        ("Justin Bieber", "Daniel Caesar", "GIVĒON"),
        album="Justice",
        duration_ms=198_081,
        isrc="USUM72102647",
        explicit=False,
    )

    assert SpotifyProvider.resolve_search_candidates(requested, (explicit, clean)) is None
    assert (
        SpotifyProvider.resolve_explicit_preference(requested, (explicit, clean), "prefer_explicit")
        == explicit
    )
    assert (
        SpotifyProvider.resolve_explicit_preference(requested, (explicit, clean), "prefer_clean")
        == clean
    )
    known_clean = replace(requested, explicit=False)
    assert (
        SpotifyProvider.resolve_explicit_preference(
            known_clean, (explicit, clean), "prefer_explicit"
        )
        == clean
    )


def test_spotify_provider_matches_youtube_cover_using_the_cover_artist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Drops of Jupiter First to Eleven"
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "cover",
                            "name": "Drops of Jupiter",
                            "artists": [{"name": "First To Eleven"}],
                        }
                    ]
                }
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack(
            "youtube_music:cover",
            '"Drops of Jupiter" - Train (Cover by First to Eleven)',
            ("First To Eleven",),
        )
    )

    assert resolved is not None
    assert resolved.provider_track_id == "spotify:cover"


def test_spotify_provider_keeps_requested_acoustic_versions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "standard",
                            "name": "Bad Habits",
                            "artists": [{"name": "Ed Sheeran"}],
                        },
                        {
                            "id": "acoustic",
                            "name": "Bad Habits (Acoustic Version)",
                            "artists": [{"name": "Ed Sheeran"}],
                        },
                    ]
                }
            },
        )

    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    resolved = provider.search_track(
        ProviderTrack(
            "youtube_music:acoustic",
            "Ed Sheeran - Bad Habits (Acoustic Version)",
            ("Ed Sheeran - Topic",),
        )
    )

    assert resolved is not None
    assert resolved.provider_track_id == "spotify:acoustic"


def test_spotify_provider_does_not_search_for_unavailable_youtube_videos() -> None:
    provider = SpotifyProvider(
        access_token="token",
        client=httpx.Client(
            base_url="https://api.spotify.com/v1",
            transport=httpx.MockTransport(lambda _: pytest.fail("search should not be called")),
        ),
    )

    assert (
        provider.search_track(ProviderTrack("youtube_music:private", "Private video", ())) is None
    )
