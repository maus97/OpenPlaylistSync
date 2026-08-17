"""Spotify provider adapter with injectable HTTP transport."""

from collections.abc import Sequence

import httpx

from ops.providers.types import ProviderPlaylist, ProviderTrack


class SpotifyProvider:
    """Spotify adapter with injectable HTTP transport and guarded writes."""

    name = "spotify"

    def __init__(self, access_token: str | None = None, client: httpx.Client | None = None) -> None:
        self.access_token = access_token
        self.client = client or httpx.Client(base_url="https://api.spotify.com/v1", timeout=20)

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise ValueError("Spotify access token is required")
        return {"Authorization": f"Bearer {self.access_token}"}

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        response = self.client.get("/me/playlists", headers=self._headers(), params={"limit": 50})
        response.raise_for_status()
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"spotify:{item['id']}",
                name=item["name"],
                tracks=(),
            )
            for item in response.json().get("items", [])
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = playlist_id.removeprefix("spotify:")
        response = self.client.get(
            f"/playlists/{raw_id}",
            headers=self._headers(),
            params={
                "fields": (
                    "id,name,description,"
                    "tracks.items(track(id,name,artists(name),album(name),duration_ms,"
                    "external_ids(isrc)))"
                )
            },
        )
        response.raise_for_status()
        payload = response.json()
        tracks = []
        for item in payload.get("tracks", {}).get("items", []):
            track = item.get("track") or {}
            if not track.get("id"):
                continue
            tracks.append(
                ProviderTrack(
                    provider_track_id=f"spotify:{track['id']}",
                    title=track.get("name", ""),
                    artists=tuple(artist.get("name", "") for artist in track.get("artists", [])),
                    album=(track.get("album") or {}).get("name"),
                    duration_ms=track.get("duration_ms"),
                    isrc=(track.get("external_ids") or {}).get("isrc"),
                )
            )
        return ProviderPlaylist(
            provider_playlist_id=f"spotify:{payload['id']}",
            name=payload.get("name", ""),
            description=payload.get("description"),
            tracks=tuple(tracks),
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        query = f'track:"{track.title}" artist:"{track.artists[0] if track.artists else ""}"'
        response = self.client.get(
            "/search",
            headers=self._headers(),
            params={"q": query, "type": "track", "limit": 1},
        )
        response.raise_for_status()
        item = (response.json().get("tracks", {}).get("items") or [None])[0]
        if not item:
            return None
        return ProviderTrack(
            provider_track_id=f"spotify:{item['id']}",
            title=item.get("name", ""),
            artists=tuple(artist.get("name", "") for artist in item.get("artists", [])),
            album=(item.get("album") or {}).get("name"),
            duration_ms=item.get("duration_ms"),
            isrc=(item.get("external_ids") or {}).get("isrc"),
        )

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        profile = self.client.get("/me", headers=self._headers())
        profile.raise_for_status()
        response = self.client.post(
            f"/users/{profile.json()['id']}/playlists",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": name, "description": description or "", "public": False},
        )
        response.raise_for_status()
        payload = response.json()
        return ProviderPlaylist(
            provider_playlist_id=f"spotify:{payload['id']}", name=payload["name"], tracks=()
        )

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("spotify:")
        uris = [
            f"spotify:track:{track.provider_track_id.removeprefix('spotify:')}" for track in tracks
        ]
        if not uris:
            return
        response = self.client.post(
            f"/playlists/{raw_id}/tracks",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"uris": uris},
        )
        response.raise_for_status()

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("spotify:")
        uris = [
            f"spotify:track:{track.provider_track_id.removeprefix('spotify:')}" for track in tracks
        ]
        if not uris:
            return
        response = self.client.request(
            "DELETE",
            f"/playlists/{raw_id}/tracks",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"tracks": [{"uri": uri} for uri in uris]},
        )
        response.raise_for_status()
