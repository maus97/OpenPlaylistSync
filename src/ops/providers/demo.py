"""Local synthetic providers for safe GUI and synchronization testing."""

from collections.abc import Sequence
from threading import RLock

from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.domain import track_key

_LOCK = RLock()
_PLAYLISTS: dict[str, ProviderPlaylist] = {}


def _track(provider: str, suffix: str, title: str, isrc: str) -> ProviderTrack:
    return ProviderTrack(
        provider_track_id=f"{provider}:demo-track-{suffix}",
        title=title,
        artists=("OPS Demo Artist",),
        album="OPS Demo Album",
        duration_ms=210000,
        isrc=isrc,
    )


def reset_demo_state() -> None:
    """Reset both synthetic providers to the same two-track playlist."""

    with _LOCK:
        for provider in ("spotify", "youtube_music"):
            _PLAYLISTS[f"{provider}:demo-playlist"] = ProviderPlaylist(
                provider_playlist_id=f"{provider}:demo-playlist",
                name="OPS Demo Mix",
                description="Synthetic playlist for local testing",
                tracks=(
                    _track(provider, "one", "Midnight Signals", "OPS-ISRC-001"),
                    _track(provider, "two", "Neon Tides", "OPS-ISRC-002"),
                ),
            )


def mutate_demo_state(change: str) -> None:
    """Apply a deterministic test change to one synthetic provider."""

    with _LOCK:
        source_id = "spotify:demo-playlist"
        target_id = "youtube_music:demo-playlist"
        if source_id not in _PLAYLISTS:
            reset_demo_state()
        if change == "source_add":
            playlist = _PLAYLISTS[source_id]
            candidate = _track("spotify", "three", "Afterglow Drive", "OPS-ISRC-003")
            if track_key(candidate) not in {track_key(item) for item in playlist.tracks}:
                _PLAYLISTS[source_id] = ProviderPlaylist(
                    provider_playlist_id=playlist.provider_playlist_id,
                    name=playlist.name,
                    description=playlist.description,
                    tracks=(*playlist.tracks, candidate),
                )
        elif change == "target_remove":
            playlist = _PLAYLISTS[target_id]
            _PLAYLISTS[target_id] = ProviderPlaylist(
                provider_playlist_id=playlist.provider_playlist_id,
                name=playlist.name,
                description=playlist.description,
                tracks=tuple(item for item in playlist.tracks if item.isrc != "OPS-ISRC-002"),
            )
        elif change == "reset":
            reset_demo_state()
        else:
            raise ValueError(f"unknown demo change: {change}")


class DemoProvider:
    """Provider adapter that never performs network I/O."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.name = provider

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        with _LOCK:
            return tuple(
                item for key, item in _PLAYLISTS.items() if key.startswith(f"{self.provider}:")
            )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        with _LOCK:
            if playlist_id not in _PLAYLISTS:
                raise ValueError(f"demo playlist not found: {playlist_id}")
            return _PLAYLISTS[playlist_id]

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        with _LOCK:
            for playlist in self.list_playlists():
                for candidate in playlist.tracks:
                    if track_key(candidate) == track_key(track):
                        return candidate
        return ProviderTrack(
            provider_track_id=f"{self.provider}:resolved-{track_key(track)}",
            title=track.title,
            artists=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
        )

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        playlist_id = f"{self.provider}:demo-created-{len(self.list_playlists()) + 1}"
        playlist = ProviderPlaylist(playlist_id, name, (), description)
        with _LOCK:
            _PLAYLISTS[playlist_id] = playlist
        return playlist

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        with _LOCK:
            playlist = self.get_playlist(playlist_id)
            existing = {track_key(item) for item in playlist.tracks}
            additions = tuple(item for item in tracks if track_key(item) not in existing)
            _PLAYLISTS[playlist_id] = ProviderPlaylist(
                playlist.provider_playlist_id,
                playlist.name,
                (*playlist.tracks, *additions),
                playlist.description,
            )

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        remove_keys = {track_key(item) for item in tracks}
        with _LOCK:
            playlist = self.get_playlist(playlist_id)
            _PLAYLISTS[playlist_id] = ProviderPlaylist(
                playlist.provider_playlist_id,
                playlist.name,
                tuple(item for item in playlist.tracks if track_key(item) not in remove_keys),
                playlist.description,
            )


reset_demo_state()
