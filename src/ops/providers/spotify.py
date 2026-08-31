"""Spotify Web API adapter with paging, scoring, and injectable HTTP transport."""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from ops.providers.base import AuthorizationRequired, ProviderUnavailable, RateLimited
from ops.providers.types import ProviderPlaylist, ProviderTrack

_DISPLAY_METADATA = re.compile(
    r"\s*(?:\(|\[)?(?:official(?: music)? (?:video|audio)|official lyric video|"
    r"(?:lyric|lyrics) video|visuali[sz]er|audio|hd|hq)(?:\)|\])?",
    re.IGNORECASE,
)
_UNAVAILABLE_TITLES = {"deleted video", "private video", "unavailable video"}
_COVER_SUFFIX = re.compile(
    r"\s*\((?:cover\s+by\s+(?P<by_artist>[^)]+)|(?P<suffix_artist>[^)]+?)\s+cover)\)\s*$",
    re.IGNORECASE,
)
_VARIANT_TOKENS = {
    "acoustic",
    "unplugged",
    "live",
    "remix",
    "remastered",
    "karaoke",
    "instrumental",
    "slowed",
    "sped",
    "nightcore",
    "cover",
}


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z0-9]+", normalized.casefold()))


def _compact(value: str) -> str:
    return "".join(_tokens(value))


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

    @staticmethod
    def _clean_channel_artist(value: str) -> str:
        """Remove upload-channel labels that are not an artist name."""

        return re.sub(r"\s*(?:[-–—]\s*)?(?:topic|vevo)\s*$", "", value, flags=re.IGNORECASE)

    @staticmethod
    def _is_official_channel(value: str) -> bool:
        return bool(re.search(r"(?:[-–—]\s*)?topic\s*$|vevo\s*$", value, re.IGNORECASE))

    @staticmethod
    def _strip_display_metadata(value: str) -> str:
        """Drop upload labels while retaining requested music variants."""

        clean = _DISPLAY_METADATA.sub("", value)
        clean = re.sub(r"\s+", " ", clean)
        return re.sub(r"^[\s\-–—]+|[\s\-–—]+$", "", clean)

    @classmethod
    def _search_metadata(cls, track: ProviderTrack) -> ProviderTrack | None:
        """Turn a YouTube upload title/channel into cautious Spotify metadata.

        The YouTube Data API exposes the uploaded video's title and channel,
        rather than catalogue-level artist/title fields. We only remove
        technical upload labels; qualifiers such as acoustic, live, and remix
        remain part of the requested title and are required by scoring below.
        """

        title = cls._strip_display_metadata(track.title)
        if " ".join(_tokens(title)) in _UNAVAILABLE_TITLES:
            return None
        source_artist = cls._clean_channel_artist(track.artists[0]) if track.artists else ""
        artists = (source_artist,) if source_artist else ()

        cover = _COVER_SUFFIX.search(title)
        if cover:
            title = title[: cover.start()].strip()
            cover_artist = cover.group("by_artist") or cover.group("suffix_artist") or ""
            artists = (cover_artist.strip(),) if cover_artist.strip() else artists
            if " - " in title:
                left, right = title.split(" - ", 1)
                title = left.strip('"“”') if left.lstrip().startswith(('"', "“")) else right
        elif track.artists and cls._is_official_channel(track.artists[0]) and " - " in title:
            title_artist, title = title.split(" - ", 1)
            if title_artist.strip():
                artists = (title_artist.strip(),)

        return ProviderTrack(
            provider_track_id=track.provider_track_id,
            title=title.strip(),
            artists=artists,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
            occurrence_id=track.occurrence_id,
            position=track.position,
        )

    @staticmethod
    def _title_tokens(value: str) -> tuple[str, ...]:
        return tuple(token for token in _tokens(value) if token not in {"version", "edit"})

    @classmethod
    def _artist_score(cls, requested: str, candidate_artists: Sequence[str]) -> float:
        requested_tokens = set(_tokens(requested))
        candidate_tokens = {token for artist in candidate_artists for token in _tokens(artist)}
        if not requested_tokens or not candidate_tokens:
            return 0.0
        if requested_tokens == candidate_tokens or _compact(requested) in {
            _compact(artist) for artist in candidate_artists
        }:
            return 30.0
        overlap = len(requested_tokens & candidate_tokens) / len(requested_tokens)
        return 25.0 if overlap >= 0.8 else 0.0

    @classmethod
    def _search_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        requested_title = cls._title_tokens(requested.title)
        candidate_title = cls._title_tokens(candidate.title)
        if not requested_title or not candidate_title:
            return 0.0

        requested_set = set(requested_title)
        candidate_set = set(candidate_title)
        if requested_title == candidate_title:
            score = 65.0
        elif len(candidate_title) >= len(requested_title) and any(
            tuple(candidate_title[offset : offset + len(requested_title)]) == requested_title
            for offset in range(len(candidate_title) - len(requested_title) + 1)
        ):
            score = 58.0
        else:
            overlap = len(requested_set & candidate_set) / len(requested_set)
            if overlap < 0.75:
                return 0.0
            score = 48.0

        if requested.artists:
            artist_score = max(
                (cls._artist_score(artist, candidate.artists) for artist in requested.artists),
                default=0.0,
            )
            if not artist_score:
                return 0.0
            score += artist_score

        requested_variants = set(requested_title) & _VARIANT_TOKENS
        candidate_variants = set(candidate_title) & _VARIANT_TOKENS
        # A cover is identified by its performer; Spotify titles rarely include
        # that word. Other requested variants must be present in the result.
        missing_variants = (requested_variants - candidate_variants) - {"cover"}
        score -= 35.0 * len(missing_variants)
        score -= 18.0 * len(candidate_variants - requested_variants)

        if requested.duration_ms and candidate.duration_ms:
            difference = abs(requested.duration_ms - candidate.duration_ms)
            if difference <= 2_500:
                score += 10.0
            elif difference > 15_000:
                score -= 25.0
        return score

    @classmethod
    def _choose_search_candidate(
        cls, requested: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        scored = sorted(
            ((cls._search_score(requested, candidate), candidate) for candidate in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < 80:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 8:
            return None
        return scored[0][1]

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        requested = self._search_metadata(track)
        if requested is None:
            return None
        query = " ".join(part for part in (requested.title, *requested.artists) if part)
        payload = self._request(
            "GET",
            "/search",
            # The search endpoint accepts an ISO country code for ``market``;
            # ``from_token`` is not valid here and now returns HTTP 400.  When
            # omitted, Spotify uses the country associated with the OAuth user.
            params={"q": query, "type": "track", "limit": 10},
        ).json()
        candidates = tuple(
            candidate
            for item in payload.get("tracks", {}).get("items", [])
            if (candidate := self._track(item)) is not None
        )
        return self._choose_search_candidate(requested, candidates)

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
