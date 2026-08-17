"""Common provider interface used by the synchronization engine."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ops.providers.types import ProviderPlaylist, ProviderTrack


@runtime_checkable
class MusicProvider(Protocol):
    """Mockable provider contract; implementations must not leak SDK types."""

    name: str

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        """List playlists visible to the authenticated provider account."""

        ...

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        """Return a normalized playlist snapshot."""

        ...

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        """Resolve a normalized track to this provider's track identity."""

        ...

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        """Create a playlist only after the sync safety policy approves it."""

        ...

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        """Add tracks to a playlist."""

        ...

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        """Remove tracks only after explicit destructive-action approval."""

        ...
