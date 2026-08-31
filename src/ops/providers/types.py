"""Provider-neutral playlist value objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderTrack:
    """A normalized track reference returned by a provider adapter."""

    provider_track_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    occurrence_id: str | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderPlaylist:
    """A normalized playlist snapshot returned by a provider adapter."""

    provider_playlist_id: str
    name: str
    tracks: tuple[ProviderTrack, ...]
    description: str | None = None
