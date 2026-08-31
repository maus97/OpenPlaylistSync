from typing import Any

import pytest

from ops.providers.types import ProviderTrack
from ops.providers.youtube_music import YouTubeMusicProvider


class FakeYTMusic:
    def __init__(self) -> None:
        self.removals: list[tuple[str, list[dict[str, str]]]] = []

    def get_library_playlists(self, *, limit: int | None) -> list[dict[str, Any]]:
        return [{"playlistId": "playlist-1", "title": "Mix"}]

    def get_playlist(self, playlist_id: str, *, limit: int | None) -> dict[str, Any]:
        return {
            "id": playlist_id,
            "title": "Mix",
            "tracks": [
                {
                    "videoId": "video-1",
                    "setVideoId": "occurrence-1",
                    "title": "Song",
                    "artists": [{"name": "Artist"}],
                    "lengthSeconds": "180",
                }
            ],
        }

    def search(self, query: str, *, filter: str) -> list[dict[str, Any]]:
        return []

    def remove_playlist_items(self, playlist_id: str, items: list[dict[str, str]]) -> None:
        self.removals.append((playlist_id, items))


def test_youtube_snapshot_preserves_occurrence_identifier_and_removes_exact_item() -> None:
    client = FakeYTMusic()
    provider = YouTubeMusicProvider(client=client)

    snapshot = provider.get_playlist("youtube_music:playlist-1")
    provider.remove_tracks("youtube_music:playlist-1", [snapshot.tracks[0]])

    assert snapshot.tracks[0].occurrence_id == "occurrence-1"
    assert client.removals == [
        ("playlist-1", [{"videoId": "video-1", "setVideoId": "occurrence-1"}])
    ]


def test_youtube_removal_rejects_item_without_occurrence_identifier() -> None:
    provider = YouTubeMusicProvider(client=FakeYTMusic())

    with pytest.raises(ValueError, match="occurrence"):
        provider.remove_tracks(
            "youtube_music:playlist-1",
            [ProviderTrack("youtube_music:video-1", "Song", ("Artist",))],
        )
