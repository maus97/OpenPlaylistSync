"""YouTube Music adapter backed by the official YouTube Data API v3.

YouTube Music playlists are exposed through the YouTube Data API. Using that
supported API avoids the unstable private YouTube Music endpoint previously
used by OPS while preserving the provider-neutral synchronization contract.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from ops.providers.base import AuthorizationRequired, ProviderUnavailable, RateLimited
from ops.providers.types import ProviderPlaylist, ProviderTrack


class YouTubeMusicProvider:
    """Official YouTube Data API adapter with an injectable HTTP transport."""

    name = "youtube_music"

    def __init__(self, access_token: str | None = None, client: httpx.Client | None = None) -> None:
        self.access_token = access_token
        self.client = client or httpx.Client(
            base_url="https://www.googleapis.com/youtube/v3", timeout=20
        )

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise AuthorizationRequired("YouTube Music account needs to be connected")
        return {"Authorization": f"Bearer {self.access_token}"}

    @staticmethod
    def _error_reason(response: httpx.Response) -> str | None:
        try:
            errors = response.json().get("error", {}).get("errors", [])
        except (TypeError, ValueError):
            return None
        if not errors:
            return None
        return errors[0].get("reason")

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("YouTube Music could not be reached") from exc
        if response.status_code == 401:
            raise AuthorizationRequired(
                "YouTube Music authorization expired; reconnect the account"
            )
        reason = self._error_reason(response)
        if response.status_code == 429:
            raise RateLimited()
        if response.status_code == 403 and reason in {
            "quotaExceeded",
            "rateLimitExceeded",
            "userRateLimitExceeded",
        }:
            raise RateLimited()
        if response.status_code == 403 and reason in {"insufficientPermissions", "forbidden"}:
            raise AuthorizationRequired("YouTube Music access was denied; reconnect the account")
        if response.status_code >= 500:
            raise ProviderUnavailable("YouTube Music is temporarily unavailable")
        if response.is_error:
            raise ProviderUnavailable("YouTube Music could not complete the request")
        return response

    @staticmethod
    def _duration_ms(value: str | None) -> int | None:
        if not value:
            return None
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
        if not match:
            return None
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return ((hours * 60 + minutes) * 60 + seconds) * 1000

    @staticmethod
    def _track(
        item: dict[str, Any],
        *,
        occurrence_id: str | None = None,
        position: int | None = None,
    ) -> ProviderTrack | None:
        video_id = item.get("id") or (item.get("contentDetails") or {}).get("videoId")
        if not video_id:
            return None
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        artist = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or ""
        return ProviderTrack(
            provider_track_id=f"youtube_music:{video_id}",
            title=snippet.get("title", ""),
            artists=(artist,) if artist else (),
            duration_ms=YouTubeMusicProvider._duration_ms(details.get("duration")),
            occurrence_id=occurrence_id,
            position=position,
        )

    def _paged(self, endpoint: str, params: dict[str, str]) -> Iterable[dict[str, Any]]:
        page_params = dict(params)
        while True:
            payload = self._request("GET", endpoint, params=page_params).json()
            yield from payload.get("items", [])
            token = payload.get("nextPageToken")
            if not token:
                return
            page_params["pageToken"] = token

    def _videos(self, video_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        videos: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(video_ids), 50):
            batch = video_ids[offset : offset + 50]
            if not batch:
                continue
            payload = self._request(
                "GET",
                "/videos",
                params={
                    "part": "snippet,contentDetails",
                    "id": ",".join(batch),
                    "maxResults": "50",
                },
            ).json()
            videos.update({item["id"]: item for item in payload.get("items", []) if item.get("id")})
        return videos

    @staticmethod
    def _search_tokens(value: str) -> tuple[str, ...]:
        normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        return tuple(re.findall(r"[a-z0-9]+", normalized.casefold()))

    @classmethod
    def _canonical_search_tokens(cls, value: str) -> tuple[str, ...]:
        """Normalize common feature abbreviations used in video titles."""

        aliases = {"ft": "feat", "featuring": "feat"}
        return tuple(aliases.get(token, token) for token in cls._search_tokens(value))

    @classmethod
    def _requested_title_tokens(cls, value: str) -> tuple[str, ...]:
        """Remove provider display metadata that is not part of a song title.

        Spotify often appends labels such as ``From "Frozen"/Soundtrack
        Version`` to the title.  Those labels are useful to a person but do
        not normally appear in the official YouTube upload, so retaining them
        makes an otherwise exact result look unrelated.
        """

        clean_title = re.sub(
            r"\s*[-–—]\s*(?:from\b.*|.*\b(?:soundtrack|version|remaster(?:ed)?)\b.*)$",
            "",
            value,
            flags=re.IGNORECASE,
        )
        return cls._canonical_search_tokens(clean_title)

    @classmethod
    def _artist_in_candidate(cls, artist: str, candidate: ProviderTrack) -> bool:
        """Recognize normal channel spelling variants such as ``ArtistVEVO``."""

        artist_tokens = cls._search_tokens(artist)
        if not artist_tokens:
            return False
        artist_set = set(artist_tokens)
        candidate_artist_tokens = {
            token
            for candidate_artist in candidate.artists
            for token in cls._search_tokens(candidate_artist)
        }
        if artist_set <= candidate_artist_tokens:
            return True
        candidate_title_tokens = set(cls._search_tokens(candidate.title))
        if artist_set <= candidate_title_tokens:
            return True
        compact_artist = "".join(artist_tokens)
        compact_channels = "".join(
            token
            for candidate_artist in candidate.artists
            for token in cls._search_tokens(candidate_artist)
        )
        return len(compact_artist) >= 4 and compact_artist in compact_channels

    @classmethod
    def _trusted_upload(cls, candidate: ProviderTrack) -> bool:
        """Identify official YouTube distribution channels without trusting uploads blindly."""

        channel_tokens = {
            token for artist in candidate.artists for token in cls._search_tokens(artist)
        }
        compact_channel = "".join(channel_tokens)
        return "topic" in channel_tokens or compact_channel.endswith("vevo")

    @classmethod
    def _variant_penalty(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        """Prefer the standard recording unless the source requests a variant."""

        requested_tokens = set(cls._canonical_search_tokens(requested.title))
        candidate_tokens = set(cls._canonical_search_tokens(candidate.title))
        penalty = 0.0
        for label, amount in (("acoustic", 20.0), ("unplugged", 20.0), ("live", 12.0)):
            if label in candidate_tokens and label not in requested_tokens:
                penalty += amount
        for labels, amount in ((("sign", "language"), 24.0), (("sing", "along"), 10.0)):
            if set(labels) <= candidate_tokens and not set(labels) <= requested_tokens:
                penalty += amount
        return penalty

    @classmethod
    def _search_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        """Score YouTube's descriptive video titles conservatively.

        Unlike Spotify, YouTube search titles commonly add labels such as
        ``Official Video`` or ``Lyrics`` and the channel name is often a Vevo
        or label name rather than the artist. The requested title must still be
        present and an artist must be represented either in the title or the
        channel metadata before a result can be accepted.
        """

        requested_title = cls._requested_title_tokens(requested.title)
        candidate_title = cls._canonical_search_tokens(candidate.title)
        if not requested_title or not candidate_title:
            return 0.0

        score = 0.0
        requested_title_set = set(requested_title)
        candidate_title_set = set(candidate_title)
        if requested_title == candidate_title:
            score += 60.0
        elif len(candidate_title) >= len(requested_title) and any(
            tuple(candidate_title[offset : offset + len(requested_title)]) == requested_title
            for offset in range(len(candidate_title) - len(requested_title) + 1)
        ):
            score += 55.0
        else:
            overlap = len(requested_title_set & candidate_title_set) / len(requested_title_set)
            if overlap >= 0.8:
                score += 45.0
            elif overlap >= 0.5:
                score += 20.0

        artist_match = False
        for artist in requested.artists:
            if cls._artist_in_candidate(artist, candidate):
                score += 30.0 / max(len(requested.artists), 1)
                artist_match = True

        if not artist_match and requested.artists:
            return 0.0
        if requested.duration_ms and candidate.duration_ms:
            difference = abs(requested.duration_ms - candidate.duration_ms)
            if difference <= 2_500:
                score += 10.0
            elif difference > 15_000:
                score -= 25.0

        qualifiers = set(candidate_title)
        if "official" in qualifiers and "video" in qualifiers:
            score += 16.0
        elif "official" in qualifiers and "audio" in qualifiers:
            score += 10.0
        if "lyrics" in qualifiers:
            score -= 10.0
        if "live" in qualifiers:
            score -= 12.0
        if {"cover", "karaoke", "instrumental"} & qualifiers:
            score -= 25.0
        if "remix" in qualifiers:
            score -= 12.0
        if cls._trusted_upload(candidate):
            score += 12.0
        return score - cls._variant_penalty(requested, candidate)

    @classmethod
    def _choose_search_candidate(
        cls, requested: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        def sort_key(item: tuple[float, ProviderTrack]) -> tuple[float, int, int, str]:
            score, candidate = item
            duration_difference = (
                abs(requested.duration_ms - candidate.duration_ms)
                if requested.duration_ms is not None and candidate.duration_ms is not None
                else 1_000_000_000
            )
            # A stable, evidence-based tie-break avoids rejecting an obvious
            # recording merely because YouTube lists both an official video
            # and an official lyric upload.  It never promotes a candidate
            # below the confidence threshold.
            return (
                score,
                int(cls._trusted_upload(candidate)),
                -duration_difference,
                candidate.provider_track_id,
            )

        scored = sorted(
            ((cls._search_score(requested, candidate), candidate) for candidate in candidates),
            key=sort_key,
            reverse=True,
        )
        if not scored or scored[0][0] < 70.0:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 5.0:
            near_ties = [candidate for score, candidate in scored if scored[0][0] - score < 5.0]
            requested_title = cls._requested_title_tokens(requested.title)
            # Near ties are safe only when every candidate demonstrably
            # contains the requested song title.  This preserves the original
            # refusal to guess between genuinely different songs.
            if not all(
                any(
                    tuple(
                        cls._canonical_search_tokens(candidate.title)[
                            offset : offset + len(requested_title)
                        ]
                    )
                    == requested_title
                    for offset in range(
                        len(cls._canonical_search_tokens(candidate.title))
                        - len(requested_title)
                        + 1
                    )
                )
                for candidate in near_ties
            ):
                return None
        return scored[0][1]

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"youtube_music:{item['id']}",
                name=(item.get("snippet") or {}).get("title", ""),
                description=(item.get("snippet") or {}).get("description") or None,
                tracks=(),
            )
            for item in self._paged(
                "/playlists", {"part": "snippet", "mine": "true", "maxResults": "50"}
            )
            if item.get("id")
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = playlist_id.removeprefix("youtube_music:")
        playlist_payload = self._request(
            "GET", "/playlists", params={"part": "snippet", "id": raw_id, "maxResults": "1"}
        ).json()
        playlists = playlist_payload.get("items", [])
        if not playlists:
            raise ProviderUnavailable("YouTube Music playlist is no longer available")
        playlist = playlists[0]
        items = list(
            self._paged(
                "/playlistItems",
                {"part": "snippet,contentDetails", "playlistId": raw_id, "maxResults": "50"},
            )
        )
        video_ids = [
            str((item.get("contentDetails") or {}).get("videoId"))
            for item in items
            if (item.get("contentDetails") or {}).get("videoId")
        ]
        videos = self._videos(video_ids)
        tracks: list[ProviderTrack] = []
        for position, item in enumerate(items):
            video_id = (item.get("contentDetails") or {}).get("videoId")
            source = videos.get(video_id, item) if video_id else item
            track = self._track(
                source,
                occurrence_id=item.get("id"),
                position=(item.get("snippet") or {}).get("position", position),
            )
            if track is not None:
                tracks.append(track)
        snippet = playlist.get("snippet") or {}
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{playlist['id']}",
            name=snippet.get("title", ""),
            description=snippet.get("description") or None,
            tracks=tuple(tracks),
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        query = f"{track.title} {track.artists[0] if track.artists else ''}".strip()
        payload = self._request(
            "GET",
            "/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "videoCategoryId": "10",
                "maxResults": "10",
            },
        ).json()
        video_ids = [
            item.get("id", {}).get("videoId") for item in payload.get("items", []) if item.get("id")
        ]
        candidates = tuple(
            candidate
            for video in self._videos([video_id for video_id in video_ids if video_id]).values()
            if (candidate := self._track(video)) is not None
        )
        return self._choose_search_candidate(track, candidates)

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        payload = self._request(
            "POST",
            "/playlists",
            params={"part": "snippet,status"},
            headers={"Content-Type": "application/json"},
            json={
                "snippet": {"title": name, "description": description or ""},
                "status": {"privacyStatus": "private"},
            },
        ).json()
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{payload['id']}",
            name=(payload.get("snippet") or {}).get("title", name),
            description=(payload.get("snippet") or {}).get("description") or None,
            tracks=(),
        )

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        raw_id = playlist_id.removeprefix("youtube_music:")
        for track in tracks:
            self._request(
                "POST",
                "/playlistItems",
                params={"part": "snippet"},
                headers={"Content-Type": "application/json"},
                json={
                    "snippet": {
                        "playlistId": raw_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": track.provider_track_id.removeprefix("youtube_music:"),
                        },
                    }
                },
            )

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        if any(not track.occurrence_id for track in tracks):
            raise ValueError("YouTube Music removal requires the playlist item identifier")
        for track in tracks:
            self._request("DELETE", "/playlistItems", params={"id": track.occurrence_id or ""})
