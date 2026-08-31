"""Spotify Web API adapter with paging, scoring, and injectable HTTP transport."""

from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from ops.providers.base import AuthorizationRequired, ProviderUnavailable, RateLimited
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.matching import choose_best_candidate


class SpotifyProvider:
    """Spotify adapter; all provider responses are converted to neutral values."""

    name = "spotify"

    def __init__(self, access_token: str | None = None, client: httpx.Client | None = None) -> None:
        self.access_token = access_token
        self.client = client or httpx.Client(base_url="https://api.spotify.com/v1", timeout=20)

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise AuthorizationRequired("Spotify account needs to be connected")
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("Spotify could not be reached") from exc
        if response.status_code == 401:
            raise AuthorizationRequired("Spotify authorization expired; reconnect the account")
        if response.status_code == 403:
            raise AuthorizationRequired(
                "Spotify access is incomplete; reconnect Spotify and approve the requested access"
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimited(int(retry_after) if retry_after and retry_after.isdigit() else None)
        if response.status_code >= 500:
            raise ProviderUnavailable("Spotify is temporarily unavailable")
        response.raise_for_status()
        return response

    @staticmethod
    def _track(item: dict[str, Any], position: int | None = None) -> ProviderTrack | None:
        # Spotify's current playlist-items response calls the nested object
        # ``item``. Older/deprecated responses used ``track``; accepting both
        # keeps the adapter tolerant of cached or mocked provider payloads.
        track = item.get("item") or item.get("track") or item
        if not track or not track.get("id") or track.get("type", "track") != "track":
            return None
        return ProviderTrack(
            provider_track_id=f"spotify:{track['id']}",
            title=track.get("name", ""),
            artists=tuple(artist.get("name", "") for artist in track.get("artists", [])),
            album=(track.get("album") or {}).get("name"),
            duration_ms=track.get("duration_ms"),
            isrc=(track.get("external_ids") or {}).get("isrc"),
            occurrence_id=str(position) if position is not None else None,
            position=position,
        )

    def _pages(
        self, first_payload: dict[str, Any], items_key: str = "items"
    ) -> Iterable[dict[str, Any]]:
        payload = first_payload
        while True:
            yield from payload.get(items_key, [])
            next_url = payload.get("next")
            if not next_url:
                return
            payload = self._request("GET", next_url).json()

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        payload = self._request("GET", "/me/playlists", params={"limit": 50}).json()
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"spotify:{item['id']}", name=item.get("name", ""), tracks=()
            )
            for item in self._pages(payload)
            if item.get("id")
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = playlist_id.removeprefix("spotify:")
        payload = self._request(
            "GET",
            f"/playlists/{raw_id}",
            params={"fields": "id,name,description", "market": "from_token"},
        ).json()
        # Spotify removed the old ``/tracks`` playlist endpoint in favor of
        # ``/items``. The nested track object is now named ``item``.
        tracks_payload = self._request(
            "GET",
            f"/playlists/{raw_id}/items",
            params={
                "fields": (
                    "items(item(id,name,artists(name),album(name),duration_ms,"
                    "external_ids,is_local,type)),next"
                ),
                "market": "from_token",
                "limit": "50",
            },
        ).json()
        tracks: list[ProviderTrack] = []
        for position, item in enumerate(self._pages(tracks_payload)):
            track = self._track(item, position)
            if track is not None:
                tracks.append(track)
        return ProviderPlaylist(
            provider_playlist_id=f"spotify:{payload['id']}",
            name=payload.get("name", ""),
            description=payload.get("description"),
            tracks=tuple(tracks),
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        query = f'track:"{track.title}" artist:"{track.artists[0] if track.artists else ""}"'
        payload = self._request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": 10, "market": "from_token"},
        ).json()
        candidates = tuple(
            candidate
            for item in payload.get("tracks", {}).get("items", [])
            if (candidate := self._track(item)) is not None
        )
        return choose_best_candidate(track, candidates)

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        payload = self._request(
            "POST",
            "/me/playlists",
            headers={"Content-Type": "application/json"},
            json={"name": name, "description": description or "", "public": False},
        ).json()
        return ProviderPlaylist(
            provider_playlist_id=f"spotify:{payload['id']}", name=payload["name"], tracks=()
        )

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("spotify:")
        for offset in range(0, len(tracks), 100):
            uris = [
                f"spotify:track:{track.provider_track_id.removeprefix('spotify:')}"
                for track in tracks[offset : offset + 100]
            ]
            if uris:
                self._request(
                    "POST",
                    f"/playlists/{raw_id}/items",
                    headers={"Content-Type": "application/json"},
                    json={"uris": uris},
                )

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("spotify:")
        for offset in range(0, len(tracks), 100):
            removal_items = []
            for track in tracks[offset : offset + 100]:
                # The current ``/items`` endpoint accepts item URIs. The
                # removed ``/tracks`` endpoint accepted positions, but
                # sending that legacy shape to ``/items`` is not supported.
                removal_items.append(
                    {"uri": f"spotify:track:{track.provider_track_id.removeprefix('spotify:')}"}
                )
            if removal_items:
                self._request(
                    "DELETE",
                    f"/playlists/{raw_id}/items",
                    headers={"Content-Type": "application/json"},
                    json={"items": removal_items},
                )
