"""Spotify Web API adapter with paging, scoring, and injectable HTTP transport."""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ops.providers.base import AuthorizationRequired, ProviderUnavailable
from ops.providers.errors import NetworkFailure, PermissionDenied, http_failure
from ops.providers.types import (
    AutomaticCandidateMatch,
    ProviderPlaylist,
    ProviderTrack,
    ScoredCandidate,
)

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
    "guitar",
    "piano",
    "orchestral",
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
        self._token_refresher = None
        self.client = client or httpx.Client(base_url="https://api.spotify.com/v1", timeout=20)
        # A review may ask for both an automatic decision and a human fallback
        # for the same track.  Keep the provider read to one request in that
        # case; this is especially important for larger first-sync reviews.
        self._search_candidates_cache: dict[
            tuple[str, tuple[str, ...], str | None, int | None], tuple[ProviderTrack, ...]
        ] = {}

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise AuthorizationRequired("Spotify account needs to be connected")
        return {"Authorization": f"Bearer {self.access_token}"}

    def set_token_refresher(self, refresher) -> None:
        self._token_refresher = refresher

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        renewed = False
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkFailure("Spotify could not be reached") from exc
        if response.status_code == 401 and self._token_refresher is not None:
            self.access_token = self._token_refresher(self.access_token)
            renewed = True
            headers.update(self._headers())
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise NetworkFailure("Spotify could not be reached") from exc
        if response.status_code == 401:
            exc = AuthorizationRequired("Spotify authorization expired; reconnect the account")
            if renewed:
                exc.operation_started_at = datetime.now(UTC)
            raise exc
        if response.status_code == 403:
            raise http_failure(403, write=method.upper() not in {"GET", "HEAD"})
        if response.status_code == 429:
            raise http_failure(429, retry=response.headers.get("Retry-After"))
        if response.status_code >= 500:
            raise ProviderUnavailable("Spotify is temporarily unavailable")
        if response.status_code >= 400:
            raise http_failure(response.status_code, write=method.upper() not in {"GET", "HEAD"})
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
            explicit=track.get("explicit") if isinstance(track.get("explicit"), bool) else None,
        )

    def _pages(
        self, first_payload: dict[str, Any], items_key: str = "items"
    ) -> Iterable[dict[str, Any]]:
        payload = first_payload
        seen_urls: set[str] = set()
        received = 0
        expected_total: int | None = None
        for _page in range(1000):
            if not isinstance(payload, dict) or not isinstance(payload.get(items_key), list):
                raise ProviderUnavailable("Spotify returned an incomplete playlist response")
            items = payload[items_key]
            if any(not isinstance(item, dict) for item in items):
                raise ProviderUnavailable("Spotify returned an unreadable playlist entry")
            total = payload.get("total")
            if total is not None:
                if type(total) is not int or total < 0:
                    raise ProviderUnavailable("Spotify returned an invalid playlist count")
                if expected_total is not None and total != expected_total:
                    raise ProviderUnavailable("Spotify playlist changed while it was being read")
                expected_total = total
            offset = payload.get("offset")
            if offset is not None and (type(offset) is not int or offset != received):
                raise ProviderUnavailable("Spotify returned an incomplete playlist page")
            received += len(items)
            yield from items
            next_url = payload.get("next")
            if next_url is None or next_url == "":
                if expected_total is not None and received != expected_total:
                    raise ProviderUnavailable("Spotify returned an incomplete playlist response")
                return
            try:
                parsed = urlsplit(next_url) if isinstance(next_url, str) else None
                trusted = bool(
                    parsed
                    and parsed.scheme == "https"
                    and parsed.hostname == "api.spotify.com"
                    and parsed.port in (None, 443)
                    and not parsed.username
                    and not parsed.password
                    and parsed.path.startswith("/v1/")
                )
            except ValueError:
                trusted = False
            if not trusted:
                raise ProviderUnavailable("Spotify returned an invalid pagination URL")
            if not items or next_url in seen_urls:
                raise ProviderUnavailable("Spotify returned a repeated or empty playlist page")
            seen_urls.add(next_url)
            payload = self._request("GET", next_url).json()
        raise ProviderUnavailable("Spotify returned too many playlist pages")

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
        try:
            payload = self._request(
                "GET",
                f"/playlists/{raw_id}",
                params={"fields": "id,name,description,snapshot_id", "market": "from_token"},
            ).json()
        except PermissionDenied as exc:
            raise PermissionDenied(
                "Spotify denied access to this playlist. Make sure it is shared with the "
                "connected Spotify account and that playlist scopes are granted."
            ) from exc
        if not isinstance(payload, dict) or payload.get("id") != raw_id:
            raise ProviderUnavailable("Spotify returned an invalid playlist response")
        # Spotify can reveal basic details for a followed or listening-only
        # shared playlist but still forbid its items. Keep that distinction in
        # the UI: reauthorizing cannot grant ownership/collaborator status.
        try:
            # Spotify removed the old ``/tracks`` playlist endpoint in favor of
            # ``/items``. The nested track object is now named ``item``.
            tracks_payload = self._request(
                "GET",
                f"/playlists/{raw_id}/items",
                params={
                    "fields": (
                        "items(item(id,name,artists(name),album(name),duration_ms,explicit,"
                        "external_ids,is_local,type)),next,total,offset"
                    ),
                    "market": "from_token",
                    "limit": "50",
                },
            ).json()
        except PermissionDenied as exc:
            raise PermissionDenied(
                "Spotify allows OPS to see this playlist but not read its tracks. "
                "The connected account must be the owner or a collaborator; "
                "a view-only share or follow cannot be synchronized through Spotify's API."
            ) from exc
        tracks: list[ProviderTrack] = []
        for position, item in enumerate(self._pages(tracks_payload)):
            raw_track = item.get("item") if "item" in item else item.get("track", item)
            if isinstance(raw_track, dict) and (
                raw_track.get("is_local") is True or raw_track.get("type") == "episode"
            ):
                # Local files and podcast episodes are not synchronizable music.
                continue
            if not isinstance(raw_track, dict) or not raw_track.get("id"):
                # Missing catalogue data is not evidence that the user removed
                # the corresponding song from this playlist.
                raise ProviderUnavailable(
                    "Spotify could not identify every playlist entry; try again later"
                )
            track = self._track(item, position)
            if track is None:
                raise ProviderUnavailable("Spotify returned an unreadable playlist entry")
            tracks.append(track)
        return ProviderPlaylist(
            provider_playlist_id=f"spotify:{payload['id']}",
            name=payload.get("name", ""),
            description=payload.get("description"),
            tracks=tuple(tracks),
            snapshot_id=payload.get("snapshot_id"),
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
        """Normalize any legacy YouTube upload title/channel for Spotify search.

        The ytmusicapi adapter supplies catalogue metadata. This compatibility
        cleanup is retained for old persisted reviews and malformed provider
        values; it removes only technical display labels, while acoustic, live,
        and remix qualifiers remain required by scoring below.
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
        elif " - " in title:
            title_artist, possible_title = title.split(" - ", 1)
            # Official upload channels commonly put ``Artist - Song`` in the
            # title.  Artist-operated channels do the same, so do not depend
            # solely on a fragile Topic/VEVO suffix.  Strip the prefix only
            # when it agrees with the supplied artist to avoid changing a song
            # title that merely contains a dash.
            prefix_matches_artist = bool(
                artists
                and _compact(title_artist) == _compact(artists[0])
                and _compact(title_artist)
            )
            if track.artists and (
                cls._is_official_channel(track.artists[0]) or prefix_matches_artist
            ):
                title = possible_title
                if title_artist.strip() and cls._is_official_channel(track.artists[0]):
                    artists = (title_artist.strip(),)

        return ProviderTrack(
            provider_track_id=track.provider_track_id,
            title=title.strip(),
            artists=artists,
            album=track.album,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
            occurrence_id=track.occurrence_id,
            position=track.position,
            explicit=track.explicit,
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
        # Exact release metadata is strong positive evidence when two catalog
        # recordings otherwise share a title, artist, and near-identical
        # duration. Do not penalize a different album because compilations and
        # reissues frequently contain the same ISRC.
        if (
            requested.album
            and candidate.album
            and _compact(requested.album) == _compact(candidate.album)
        ):
            score += 12.0
        if requested.explicit is not None and candidate.explicit is not None:
            score += 4.0 if requested.explicit == candidate.explicit else -35.0
        return score

    @staticmethod
    def _recording_key(candidate: ProviderTrack) -> tuple[str, ...]:
        """Group equivalent Spotify catalogue listings before comparing scores.

        Spotify search often returns the same recording from several albums or
        editorial compilations.  Those entries are not an ambiguous choice for
        playlist synchronization when they share an ISRC.
        """

        if candidate.isrc:
            return ("isrc", candidate.isrc.casefold())
        return (
            "metadata",
            _compact(candidate.title),
            ",".join(sorted(_compact(artist) for artist in candidate.artists)),
            str(candidate.duration_ms or ""),
        )

    @classmethod
    def _ranked_candidates(
        cls, requested: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> tuple[tuple[float, ProviderTrack], ...]:
        """Score and collapse duplicate Spotify catalogue entries."""

        ranked = sorted(
            ((cls._search_score(requested, candidate), candidate) for candidate in candidates),
            key=lambda item: (
                item[0],
                -abs((requested.duration_ms or 0) - (item[1].duration_ms or 0)),
                item[1].provider_track_id,
            ),
            reverse=True,
        )
        unique: dict[tuple[str, ...], tuple[float, ProviderTrack]] = {}
        for score, candidate in ranked:
            unique.setdefault(cls._recording_key(candidate), (score, candidate))
        return tuple(unique.values())

    @classmethod
    def _choose_search_candidate(
        cls,
        requested: ProviderTrack,
        candidates: Sequence[ProviderTrack],
        *,
        explicit_preference: str = "no_preference",
    ) -> ProviderTrack | None:
        scored = cls._ranked_candidates(requested, candidates)
        if not scored or scored[0][0] < 80:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 8:
            if requested.explicit is None and explicit_preference in {
                "prefer_explicit",
                "prefer_clean",
            }:
                preferred_rating = explicit_preference == "prefer_explicit"
                preferred = next(
                    (
                        candidate
                        for score, candidate in scored
                        if scored[0][0] - score < 8 and candidate.explicit is preferred_rating
                    ),
                    None,
                )
                if preferred is not None:
                    return preferred
            return None
        return scored[0][1]

    def _search_candidates(self, requested: ProviderTrack) -> tuple[ProviderTrack, ...]:
        key = (requested.title, requested.artists, requested.album, requested.duration_ms)
        cached = self._search_candidates_cache.get(key)
        if cached is not None:
            return cached
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
        self._search_candidates_cache[key] = candidates
        return candidates

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        requested = self._search_metadata(track)
        if requested is None:
            return None
        return self._choose_search_candidate(requested, self._search_candidates(requested))

    @classmethod
    def resolve_search_candidates(
        cls, track: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        """Re-score an existing result set without spending another API request."""

        requested = cls._search_metadata(track)
        return cls._choose_search_candidate(requested, candidates) if requested else None

    @classmethod
    def resolve_explicit_preference(
        cls, track: ProviderTrack, candidates: Sequence[ProviderTrack], preference: str
    ) -> ProviderTrack | None:
        """Break an otherwise-safe clean/explicit tie using the operator preference.

        The preference is never applied when the source recording provides its
        own rating, and it cannot outweigh the existing artist/version scoring.
        """

        requested = cls._search_metadata(track)
        return (
            cls._choose_search_candidate(requested, candidates, explicit_preference=preference)
            if requested
            else None
        )

    @classmethod
    def best_available_match(
        cls, track: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> AutomaticCandidateMatch | None:
        """Choose the strongest plausible Spotify result after strict matching ties."""

        requested = cls._search_metadata(track)
        if requested is None:
            return None
        ranked = tuple(
            ScoredCandidate(candidate, score)
            for score, candidate in cls._ranked_candidates(requested, candidates)
            if score >= 70.0
        )
        if not ranked:
            return None
        return AutomaticCandidateMatch(
            selected=ranked[0].track,
            score=ranked[0].score,
            alternatives=ranked[1:5],
            reason=(
                "top candidates had similar Spotify matching scores"
                if len(ranked) > 1 and ranked[0].score - ranked[1].score < 8.0
                else "the best viable Spotify result was below the strict confidence threshold"
            ),
        )

    def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
        """Offer only plausible distinct Spotify recordings for human review."""

        requested = self._search_metadata(track)
        if requested is None:
            return ()
        scored = self._ranked_candidates(requested, self._search_candidates(requested))
        return tuple(candidate for score, candidate in scored if score >= 70.0)[:5]

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

    @staticmethod
    def _response_snapshot(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        value = payload.get("snapshot_id") if isinstance(payload, dict) else None
        return str(value) if value else None

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str | None:
        raw_id = playlist_id.removeprefix("spotify:")
        snapshot_id: str | None = None
        for offset in range(0, len(tracks), 100):
            uris = [
                f"spotify:track:{track.provider_track_id.removeprefix('spotify:')}"
                for track in tracks[offset : offset + 100]
            ]
            if uris:
                response = self._request(
                    "POST",
                    f"/playlists/{raw_id}/items",
                    headers={"Content-Type": "application/json"},
                    json={"uris": uris},
                )
                snapshot_id = self._response_snapshot(response) or snapshot_id
        return snapshot_id

    def remove_tracks(
        self,
        playlist_id: str,
        tracks: Sequence[ProviderTrack],
        *,
        snapshot_id: str | None = None,
    ) -> str | None:
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
                body: dict[str, Any] = {"items": removal_items}
                if snapshot_id:
                    body["snapshot_id"] = snapshot_id
                response = self._request(
                    "DELETE",
                    f"/playlists/{raw_id}/items",
                    headers={"Content-Type": "application/json"},
                    json=body,
                )
                snapshot_id = self._response_snapshot(response) or snapshot_id
        return snapshot_id
