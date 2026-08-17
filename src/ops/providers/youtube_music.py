"""YouTube Music provider adapter backed by ytmusicapi."""

from collections.abc import Sequence

from ytmusicapi import YTMusic

from ops.providers.types import ProviderPlaylist, ProviderTrack


class YouTubeMusicProvider:
    """ytmusicapi adapter with an injectable client and guarded writes."""

    name = "youtube_music"

    def __init__(self, auth: str | dict | None = None, client: YTMusic | None = None) -> None:
        self.client = client or YTMusic(auth=auth)

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"youtube_music:{item['playlistId']}",
                name=item.get("title", ""),
                tracks=(),
            )
            for item in self.client.get_library_playlists(limit=100)
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = playlist_id.removeprefix("youtube_music:")
        payload = self.client.get_playlist(raw_id)
        tracks = []
        for item in payload.get("tracks", []):
            video_id = item.get("videoId")
            if not video_id:
                continue
            tracks.append(
                ProviderTrack(
                    provider_track_id=f"youtube_music:{video_id}",
                    title=item.get("title", ""),
                    artists=tuple(artist.get("name", "") for artist in item.get("artists", [])),
                    album=(item.get("album") or {}).get("name"),
                    duration_ms=item.get("lengthSeconds", 0) * 1000 or None,
                )
            )
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{payload.get('id', raw_id)}",
            name=payload.get("title", ""),
            tracks=tuple(tracks),
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        query = f"{track.title} {track.artists[0] if track.artists else ''}".strip()
        items = self.client.search(query, filter="songs")
        item = (items or [None])[0]
        if not item or not item.get("videoId"):
            return None
        return ProviderTrack(
            provider_track_id=f"youtube_music:{item['videoId']}",
            title=item.get("title", ""),
            artists=tuple(artist.get("name", "") for artist in item.get("artists", [])),
            album=(item.get("album") or {}).get("name"),
            duration_ms=item.get("lengthSeconds", 0) * 1000 or None,
        )

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        playlist_id = self.client.create_playlist(name, description or "", "PRIVATE")
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
            self.client.add_playlist_items(raw_id, video_ids)

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("youtube_music:")
        video_ids = [track.provider_track_id.removeprefix("youtube_music:") for track in tracks]
        if video_ids:
            self.client.remove_playlist_items(raw_id, video_ids)
