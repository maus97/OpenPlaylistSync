"""YouTube Music adapter backed by ytmusicapi with occurrence-aware removal."""

from collections.abc import Sequence
from typing import Any

from ytmusicapi import OAuthCredentials, YTMusic

from ops.providers.base import AuthorizationRequired, ProviderUnavailable
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.matching import choose_best_candidate


class YouTubeMusicProvider:
    """ytmusicapi adapter with an injectable client and guarded writes."""

    name = "youtube_music"

    def __init__(
        self,
        auth: str | dict[str, Any] | None = None,
        client: YTMusic | None = None,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
    ) -> None:
        oauth_credentials = None
        if oauth_client_id or oauth_client_secret:
            if not oauth_client_id or not oauth_client_secret:
                raise ValueError(
                    "YouTube Music OAuth client ID and secret must be configured together"
                )
            oauth_credentials = OAuthCredentials(oauth_client_id, oauth_client_secret)
        self.client = client or YTMusic(auth=auth, oauth_credentials=oauth_credentials)

    @staticmethod
    def _track(item: dict[str, Any], position: int | None = None) -> ProviderTrack | None:
        video_id = item.get("videoId")
        if not video_id:
            return None
        length = item.get("lengthSeconds")
        try:
            duration_ms = int(length) * 1000 if length else None
        except (TypeError, ValueError):
            duration_ms = None
        return ProviderTrack(
            provider_track_id=f"youtube_music:{video_id}",
            title=item.get("title", ""),
            artists=tuple(artist.get("name", "") for artist in item.get("artists", [])),
            album=(item.get("album") or {}).get("name"),
            duration_ms=duration_ms,
            occurrence_id=item.get("setVideoId"),
            position=position,
        )

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.client, operation)(*args, **kwargs)
        except Exception as exc:  # ytmusicapi exposes provider-specific exception types.
            message = str(exc).casefold()
            if "unauthor" in message or "token" in message or "login" in message:
                raise AuthorizationRequired(
                    "YouTube Music authorization expired; reconnect the account"
                ) from exc
            raise ProviderUnavailable("YouTube Music could not complete the request") from exc

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        items = self._call("get_library_playlists", limit=None)
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"youtube_music:{item['playlistId']}",
                name=item.get("title", ""),
                tracks=(),
            )
            for item in items
            if item.get("playlistId")
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = playlist_id.removeprefix("youtube_music:")
        try:
            payload = self._call("get_playlist", raw_id, limit=None)
        except ProviderUnavailable as exc:
            # Older ytmusicapi versions do not accept None for the limit.
            if "could not complete" not in str(exc):
                raise
            payload = self._call("get_playlist", raw_id, limit=10_000)
        tracks = tuple(
            track
            for position, item in enumerate(payload.get("tracks", []))
            if (track := self._track(item, position)) is not None
        )
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{payload.get('id', raw_id)}",
            name=payload.get("title", ""),
            tracks=tracks,
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        query = f"{track.title} {track.artists[0] if track.artists else ''}".strip()
        items = self._call("search", query, filter="songs")
        candidates = tuple(
            candidate for item in items or () if (candidate := self._track(item)) is not None
        )
        return choose_best_candidate(track, candidates)

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        playlist_id = self._call("create_playlist", name, description or "", "PRIVATE")
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{playlist_id}",
            name=name,
            description=description,
            tracks=(),
        )

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("youtube_music:")
        video_ids = [track.provider_track_id.removeprefix("youtube_music:") for track in tracks]
        if video_ids:
            self._call("add_playlist_items", raw_id, video_ids)

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("youtube_music:")
        items = [
            {
                "videoId": track.provider_track_id.removeprefix("youtube_music:"),
                "setVideoId": track.occurrence_id,
            }
            for track in tracks
            if track.occurrence_id
        ]
        if len(items) != len(tracks):
            raise ValueError("YouTube Music removal requires the playlist occurrence identifier")
        if items:
            self._call("remove_playlist_items", raw_id, items)
