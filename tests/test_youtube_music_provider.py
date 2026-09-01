import pytest
import requests
from ytmusicapi.exceptions import YTMusicServerError

from ops.providers.base import AuthorizationRequired, RateLimited, TrackUnavailable
from ops.providers.types import ProviderTrack
from ops.providers.youtube_music import YouTubeMusicProvider, YTMusicApiProvider


def _credentials() -> dict[str, object]:
    return {
        "auth_scheme": "ytmusicapi_oauth",
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "scope": "https://www.googleapis.com/auth/youtube",
        "token_type": "Bearer",
        "expires_at": 4_102_444_800,
        "expires_in": 3600,
    }


class FakeYTMusic:
    def __init__(self) -> None:
        self.search_results: list[dict[str, object]] = []
        self.library_playlists: list[dict[str, object]] = []
        self.playlists: dict[str, dict[str, object]] = {}
        self.account_info: dict[str, object] = {
            "channelHandle": "@listener",
            "accountName": "Listener",
        }
        self.search_calls: list[tuple[str, str | None, int]] = []
        self.add_calls: list[tuple[str, list[str], bool]] = []
        self.remove_calls: list[tuple[str, list[dict[str, object]]]] = []
        self.edit_calls: list[tuple[str, dict[str, object]]] = []
        self.created: list[tuple[str, str, str]] = []

    def search(
        self,
        query: str,
        filter: str | None = None,
        scope: str | None = None,
        limit: int = 20,
        ignore_spelling: bool = False,
    ) -> list[dict[str, object]]:
        del scope, ignore_spelling
        self.search_calls.append((query, filter, limit))
        return self.search_results

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, object]]:
        assert limit is None
        return self.library_playlists

    def get_playlist(
        self,
        playlistId: str,
        limit: int | None = 100,
        related: bool = False,
        suggestions_limit: int = 0,
    ) -> dict[str, object]:
        assert limit is None
        assert not related
        assert suggestions_limit == 0
        return self.playlists[playlistId]

    def get_account_info(self) -> dict[str, object]:
        return self.account_info

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str:
        assert privacy_status == "PRIVATE"
        assert video_ids is None
        assert source_playlist is None
        self.created.append((title, description, privacy_status))
        return "new-playlist"

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> str:
        assert source_playlist is None
        self.add_calls.append((playlistId, list(videoIds or ()), duplicates))
        return "ok"

    def remove_playlist_items(self, playlistId: str, videos: list[dict[str, object]]) -> str:
        self.remove_calls.append((playlistId, videos))
        return "ok"

    def edit_playlist(
        self,
        playlistId: str,
        title: str | None = None,
        description: str | None = None,
        privacyStatus: str | None = None,
        collaboration: bool | None = None,
        moveItem: str | tuple[str, str] | None = None,
        addPlaylistId: str | None = None,
        sortOrder=None,
        addToTop: bool | None = None,
        voteOption=None,
    ) -> str:
        del sortOrder, voteOption
        self.edit_calls.append(
            (
                playlistId,
                {
                    "title": title,
                    "description": description,
                    "privacyStatus": privacyStatus,
                    "collaboration": collaboration,
                    "moveItem": moveItem,
                    "addPlaylistId": addPlaylistId,
                    "addToTop": addToTop,
                },
            )
        )
        return "ok"


def _provider(library: FakeYTMusic, catalogue: FakeYTMusic) -> YTMusicApiProvider:
    return YTMusicApiProvider(
        _credentials(),
        client_id="client-id",
        client_secret="client-secret",
        library_client=library,
        catalog_client=catalogue,
        sleep=lambda _: None,
    )


def test_ytmusicapi_maps_library_playlists_tracks_and_song_searches() -> None:
    library = FakeYTMusic()
    library.library_playlists = [{"playlistId": "playlist-1", "title": "Mix"}]
    library.playlists["playlist-1"] = {
        "id": "playlist-1",
        "title": "Mix",
        "tracks": [
            {
                "videoId": "video-1",
                "setVideoId": "occurrence-1",
                "title": "Song",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "duration_seconds": 180,
            }
        ],
    }
    catalogue = FakeYTMusic()
    catalogue.search_results = [
        {
            "videoId": "video-1",
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album"},
            "duration_seconds": 180,
            "isExplicit": True,
        }
    ]
    provider = _provider(library, catalogue)

    playlists = provider.list_playlists()
    snapshot = provider.get_playlist("youtube_music:playlist-1")
    resolved = provider.search_track(
        ProviderTrack(
            "spotify:track-1",
            "Song",
            ("Artist",),
            album="Album",
            duration_ms=180_000,
            explicit=True,
        )
    )

    assert playlists[0].provider_playlist_id == "youtube_music:playlist-1"
    assert snapshot.tracks[0].occurrence_id == "occurrence-1"
    assert snapshot.tracks[0].duration_ms == 180_000
    assert snapshot.tracks[0].album == "Album"
    assert resolved is not None
    assert resolved.provider_track_id == "youtube_music:video-1"
    assert resolved.explicit is True
    assert catalogue.search_calls == [("Song Artist", "songs", 12)]


def test_ytmusicapi_searches_with_a_catalogue_client_not_the_library_client() -> None:
    library = FakeYTMusic()

    def forbidden_search(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("account client must not be used for catalogue search")

    library.search = forbidden_search  # type: ignore[method-assign]
    catalogue = FakeYTMusic()
    catalogue.search_results = [
        {
            "videoId": "song",
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "duration_seconds": 180,
        }
    ]

    resolved = _provider(library, catalogue).search_track(
        ProviderTrack("spotify:song", "Song", ("Artist",), duration_ms=180_000)
    )

    assert resolved is not None
    assert len(catalogue.search_calls) == 1


def test_ytmusicapi_writes_duplicates_and_removes_the_exact_occurrence() -> None:
    library = FakeYTMusic()
    provider = _provider(library, FakeYTMusic())
    first = ProviderTrack("youtube_music:video-1", "Song", ("Artist",), occurrence_id="set-1")
    duplicate = ProviderTrack("youtube_music:video-1", "Song", ("Artist",), occurrence_id="set-2")

    created = provider.create_playlist("New", "A description")
    provider.add_tracks("youtube_music:new-playlist", [first, duplicate])
    provider.remove_tracks("youtube_music:new-playlist", [duplicate])
    provider.update_playlist("youtube_music:new-playlist", name="Renamed")
    provider.reorder_tracks("youtube_music:new-playlist", duplicate, before=first)

    assert created.provider_playlist_id == "youtube_music:new-playlist"
    assert library.created == [("New", "A description", "PRIVATE")]
    assert library.add_calls == [("new-playlist", ["video-1", "video-1"], True)]
    assert library.remove_calls == [
        ("new-playlist", [{"videoId": "video-1", "setVideoId": "set-2"}])
    ]
    assert library.edit_calls[0] == (
        "new-playlist",
        {
            "title": "Renamed",
            "description": None,
            "privacyStatus": None,
            "collaboration": None,
            "moveItem": None,
            "addPlaylistId": None,
            "addToTop": None,
        },
    )
    assert library.edit_calls[1][1]["moveItem"] == ("set-2", "set-1")


def test_ytmusicapi_removal_refuses_an_unidentified_duplicate_occurrence() -> None:
    with pytest.raises(ValueError, match="occurrence"):
        _provider(FakeYTMusic(), FakeYTMusic()).remove_tracks(
            "youtube_music:playlist-1",
            [ProviderTrack("youtube_music:video-1", "Song", ("Artist",))],
        )
    with pytest.raises(TrackUnavailable, match="not a YouTube Music song"):
        _provider(FakeYTMusic(), FakeYTMusic()).remove_tracks(
            "youtube_music:playlist-1",
            [ProviderTrack("spotify:video-1", "Song", ("Artist",), occurrence_id="set-1")],
        )


def test_ytmusicapi_requires_a_current_encrypted_connection_for_library_access() -> None:
    with pytest.raises(AuthorizationRequired, match="reconnected"):
        YTMusicApiProvider().list_playlists()


def test_ytmusicapi_binds_connection_to_the_channel_handle() -> None:
    library = FakeYTMusic()
    library.account_info = {"channelHandle": "@Listener", "accountName": "Listener"}

    assert _provider(library, FakeYTMusic()).account_identity() == (
        "ytmusicapi:handle:@listener",
        "Listener",
    )


def test_ytmusicapi_reports_rate_limits_without_retrying() -> None:
    class RateLimitedCatalogue(FakeYTMusic):
        def search(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise YTMusicServerError("Server returned HTTP 429: Too many requests")

    with pytest.raises(RateLimited):
        _provider(FakeYTMusic(), RateLimitedCatalogue()).search_track(
            ProviderTrack("spotify:track", "Song", ("Artist",))
        )


def test_ytmusicapi_retries_one_transient_read_but_never_a_write() -> None:
    class FlakyCatalogue(FakeYTMusic):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.attempts += 1
            if self.attempts == 1:
                raise requests.ConnectionError("temporary network failure")
            return super().search(*args, **kwargs)

    catalogue = FlakyCatalogue()
    catalogue.search_results = [
        {
            "videoId": "song",
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "duration_seconds": 180,
        }
    ]
    sleeps: list[float] = []
    provider = YTMusicApiProvider(
        _credentials(),
        client_id="client-id",
        client_secret="client-secret",
        library_client=FakeYTMusic(),
        catalog_client=catalogue,
        sleep=sleeps.append,
    )

    assert provider.search_track(ProviderTrack("spotify:song", "Song", ("Artist",))) is not None
    assert catalogue.attempts == 2
    assert sleeps == [0.25]


def test_ytmusicapi_uses_bounded_backoff_for_repeated_transient_reads() -> None:
    class FlakyCatalogue(FakeYTMusic):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.attempts += 1
            if self.attempts < 3:
                raise requests.ConnectionError("temporary network failure")
            return super().search(*args, **kwargs)

    catalogue = FlakyCatalogue()
    catalogue.search_results = [
        {
            "videoId": "song",
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "duration_seconds": 180,
        }
    ]
    sleeps: list[float] = []
    provider = YTMusicApiProvider(
        _credentials(),
        client_id="client-id",
        client_secret="client-secret",
        library_client=FakeYTMusic(),
        catalog_client=catalogue,
        sleep=sleeps.append,
    )

    assert provider.search_track(ProviderTrack("spotify:song", "Song", ("Artist",))) is not None
    assert catalogue.attempts == 3
    assert sleeps == [0.25, 0.75]


def test_ytmusicapi_treats_a_rejected_addition_as_unavailable() -> None:
    class RejectingLibrary(FakeYTMusic):
        def add_playlist_items(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise YTMusicServerError("Server returned HTTP 400: unavailable")

    with pytest.raises(TrackUnavailable):
        _provider(RejectingLibrary(), FakeYTMusic()).add_tracks(
            "youtube_music:playlist",
            [ProviderTrack("youtube_music:video", "Song", ("Artist",))],
        )


def test_ytmusicapi_prefers_the_standard_catalogue_recording_but_honors_acoustic_source() -> None:
    requested = ProviderTrack(
        "spotify:standard", "A Little More", ("Ed Sheeran",), duration_ms=192_043
    )
    standard = ProviderTrack(
        "youtube_music:standard", "A Little More", ("Ed Sheeran",), duration_ms=193_000
    )
    acoustic = ProviderTrack(
        "youtube_music:acoustic",
        "A Little More (Acoustic)",
        ("Ed Sheeran",),
        duration_ms=201_000,
    )

    assert YTMusicApiProvider._choose_search_candidate(requested, (acoustic, standard)) == standard

    requested_acoustic = ProviderTrack(
        "spotify:acoustic",
        "A Little More - Acoustic Version",
        ("Ed Sheeran",),
        duration_ms=201_000,
    )
    assert (
        YTMusicApiProvider._choose_search_candidate(requested_acoustic, (standard, acoustic))
        == acoustic
    )


def test_ytmusicapi_retains_cover_and_live_alternatives_for_manual_review() -> None:
    requested = ProviderTrack("spotify:song", "Song", ("Artist",), duration_ms=180_000)
    community_cover = ProviderTrack(
        "youtube_music:cover",
        "Song (Live Cover)",
        ("Community Artist",),
        duration_ms=182_000,
    )
    catalogue = FakeYTMusic()
    catalogue.search_results = [
        {
            "videoId": "cover",
            "title": "Song (Live Cover)",
            "artists": [{"name": "Community Artist"}],
            "duration_seconds": 182,
        }
    ]
    provider = _provider(FakeYTMusic(), catalogue)

    assert provider.search_track(requested) is None
    assert [
        candidate.provider_track_id for candidate in provider.close_track_candidates(requested)
    ] == [community_cover.provider_track_id]
    assert YTMusicApiProvider._requested_title_tokens("From the Start") == ("from", "the", "start")


def test_ytmusicapi_does_not_auto_match_an_explicit_track_to_a_clean_recording() -> None:
    requested = ProviderTrack(
        "spotify:explicit", "Song", ("Artist",), duration_ms=180_000, explicit=True
    )
    clean = ProviderTrack(
        "youtube_music:clean", "Song", ("Artist",), duration_ms=180_000, explicit=False
    )
    explicit = ProviderTrack(
        "youtube_music:explicit", "Song", ("Artist",), duration_ms=180_000, explicit=True
    )

    assert YTMusicApiProvider._choose_search_candidate(requested, (clean,)) is None
    assert YTMusicApiProvider._choose_search_candidate(requested, (clean, explicit)) == explicit


def test_existing_provider_name_is_an_alias_for_the_ytmusicapi_adapter() -> None:
    assert YouTubeMusicProvider is YTMusicApiProvider
