"""YouTube Music adapter backed by the ``ytmusicapi`` boundary.

The rest of OPS only sees provider-neutral values. ``ytmusicapi`` objects,
its non-public YouTube Music transport, and its OAuth token shape stay in this
module so a future adapter can replace it without changing synchronization
policy.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, TypeVar

import requests
from ytmusicapi import YTMusic
from ytmusicapi.auth.oauth import OAuthCredentials
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError

from ops.providers.base import (
    AuthorizationRequired,
    ProviderUnavailable,
    RateLimited,
    TrackUnavailable,
)
from ops.providers.types import ProviderPlaylist, ProviderTrack

YT_MUSIC_AUTH_SCHEME = "ytmusicapi_oauth"
YT_MUSIC_SEARCH_LIMIT = 12
_READ_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_MUTATION_BATCH_SIZE = 100
_T = TypeVar("_T")

_DISPLAY_TOKENS = frozenset({"official", "audio", "video", "music", "lyrics", "lyric", "version"})
_VARIANT_LABELS: dict[str, frozenset[str]] = {
    "acoustic": frozenset({"acoustic", "unplugged"}),
    "live": frozenset({"live", "concert", "session"}),
    "remix": frozenset({"remix", "mix"}),
    "cover": frozenset({"cover", "karaoke", "tribute"}),
    "instrumental": frozenset({"instrumental"}),
    "speed": frozenset({"sped", "slowed", "nightcore"}),
    "remaster": frozenset({"remaster", "remastered"}),
    "edit": frozenset({"edit", "extended", "demo", "mono", "stereo"}),
}
_STATUS_CODE = re.compile(r"\b(?:http|status_code:)\s*(\d{3})\b", re.IGNORECASE)


class YTMusicClient(Protocol):
    """Subset of ytmusicapi used by OPS and simple to replace in tests."""

    def search(
        self,
        query: str,
        filter: str | None = None,
        scope: str | None = None,
        limit: int = 20,
        ignore_spelling: bool = False,
    ) -> list[dict[str, Any]]: ...

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, Any]]: ...

    def get_playlist(
        self,
        playlistId: str,
        limit: int | None = 100,
        related: bool = False,
        suggestions_limit: int = 0,
    ) -> dict[str, Any]: ...

    def get_account_info(self) -> dict[str, Any]: ...

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str | dict[str, Any]: ...

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> str | dict[str, Any]: ...

    def remove_playlist_items(
        self, playlistId: str, videos: list[dict[str, Any]]
    ) -> str | dict[str, Any]: ...

    def edit_playlist(
        self,
        playlistId: str,
        title: str | None = None,
        description: str | None = None,
        privacyStatus: str | None = None,
        collaboration: bool | None = None,
        moveItem: str | tuple[str, str] | None = None,
        addPlaylistId: str | None = None,
        sortOrder: Any | None = None,
        addToTop: bool | None = None,
        voteOption: Any | None = None,
    ) -> str | dict[str, Any]: ...


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z0-9]+", normalized.casefold()))


def _compact(value: str) -> str:
    return "".join(_tokens(value))


class YTMusicApiProvider:
    """ytmusicapi implementation of OPS's YouTube Music provider contract.

    Public catalogue searches intentionally use an unauthenticated client. The
    authenticated client is created only when a library read or mutation needs
    it, reducing the exposure and request load of account credentials.
    """

    name = "youtube_music"

    def __init__(
        self,
        credentials: Mapping[str, Any] | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        library_client: YTMusicClient | None = None,
        catalog_client: YTMusicClient | None = None,
        oauth_credentials: OAuthCredentials | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = dict(credentials or {})
        self._client_id = client_id
        self._client_secret = client_secret
        self._library_client = library_client
        self._catalog_client = catalog_client
        self._oauth_credentials = oauth_credentials
        self._sleep = sleep
        self._search_candidates_cache: dict[
            tuple[str, tuple[str, ...], str | None, int | None], tuple[ProviderTrack, ...]
        ] = {}

    @staticmethod
    def _raw_id(value: str) -> str:
        return value.removeprefix("youtube_music:")

    def _oauth_token(self) -> dict[str, Any]:
        if self._credentials.get("auth_scheme") != YT_MUSIC_AUTH_SCHEME:
            raise AuthorizationRequired(
                "YouTube Music needs to be reconnected for the ytmusicapi migration"
            )
        if not self._credentials.get("access_token") or not self._credentials.get("refresh_token"):
            raise AuthorizationRequired("YouTube Music account needs to be connected")
        if not self._client_id or not self._client_secret:
            raise AuthorizationRequired("YouTube Music OAuth settings are incomplete")
        return {
            key: self._credentials[key]
            for key in (
                "scope",
                "token_type",
                "access_token",
                "refresh_token",
                "expires_at",
                "expires_in",
            )
            if key in self._credentials
        }

    def _library(self) -> YTMusicClient:
        if self._library_client is not None:
            return self._library_client
        try:
            oauth_credentials = self._oauth_credentials or OAuthCredentials(
                self._client_id or "", self._client_secret or ""
            )
            self._library_client = YTMusic(
                auth=self._oauth_token(), oauth_credentials=oauth_credentials
            )
        except (YTMusicError, ValueError, TypeError) as exc:
            raise AuthorizationRequired("YouTube Music account needs to be reconnected") from exc
        return self._library_client

    def _catalog(self) -> YTMusicClient:
        if self._catalog_client is None:
            try:
                self._catalog_client = YTMusic()
            except (YTMusicError, ValueError, TypeError) as exc:
                raise ProviderUnavailable("YouTube Music search is unavailable") from exc
        return self._catalog_client

    @staticmethod
    def _status_code(exc: BaseException) -> int | None:
        match = _STATUS_CODE.search(str(exc))
        return int(match.group(1)) if match else None

    @classmethod
    def _retryable(cls, exc: BaseException) -> bool:
        if isinstance(exc, requests.RequestException):
            return True
        return isinstance(exc, YTMusicServerError) and (cls._status_code(exc) or 0) >= 500

    @classmethod
    def _raise_provider_error(cls, exc: BaseException, *, track_write: bool) -> None:
        status_code = cls._status_code(exc)
        text = str(exc).casefold()
        if status_code in {401, 403} or "oauth" in text or "authentication" in text:
            raise AuthorizationRequired(
                "YouTube Music authorization expired; reconnect the account"
            ) from exc
        if status_code == 429 or "rate limit" in text or "too many requests" in text:
            raise RateLimited() from exc
        if track_write and status_code in {400, 404}:
            raise TrackUnavailable("YouTube Music cannot add the selected recording") from exc
        if status_code and status_code >= 500:
            raise ProviderUnavailable("YouTube Music is temporarily unavailable") from exc
        raise ProviderUnavailable("YouTube Music could not complete the request") from exc

    def _call(
        self,
        operation: Callable[[], _T],
        *,
        retry_read: bool = False,
        track_write: bool = False,
    ) -> _T:
        """Run an SDK call with short bounded backoff for transient reads only."""

        for attempt in range(len(_READ_RETRY_DELAYS_SECONDS) + 1):
            try:
                return operation()
            except (requests.RequestException, YTMusicError) as exc:
                if (
                    retry_read
                    and attempt < len(_READ_RETRY_DELAYS_SECONDS)
                    and self._retryable(exc)
                ):
                    self._sleep(_READ_RETRY_DELAYS_SECONDS[attempt])
                    continue
                self._raise_provider_error(exc, track_write=track_write)
        raise AssertionError("unreachable")

    @staticmethod
    def _duration_ms(value: object) -> int | None:
        if isinstance(value, (int, float)) and value >= 0:
            return int(float(value) * 1000)
        if isinstance(value, str):
            try:
                seconds = float(value)
            except ValueError:
                parts = value.split(":")
                if not all(part.isdigit() for part in parts) or not 1 < len(parts) <= 3:
                    return None
                seconds = 0
                for part in parts:
                    seconds = seconds * 60 + int(part)
            return int(seconds * 1000)
        return None

    @staticmethod
    def _artists(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        artists: list[str] = []
        for artist in value:
            name = artist.get("name") if isinstance(artist, dict) else artist
            if isinstance(name, str) and name.strip():
                artists.append(name.strip())
        return tuple(artists)

    @classmethod
    def _track(
        cls,
        item: Mapping[str, Any],
        *,
        occurrence_id: str | None = None,
        position: int | None = None,
    ) -> ProviderTrack | None:
        video_id = item.get("videoId") or item.get("id")
        if not isinstance(video_id, str) or not video_id:
            return None
        album = item.get("album")
        album_name = album.get("name") if isinstance(album, dict) else album
        set_video_id = occurrence_id or item.get("setVideoId")
        explicit = item.get("isExplicit")
        if not isinstance(explicit, bool):
            explicit = item.get("explicit") if isinstance(item.get("explicit"), bool) else None
        return ProviderTrack(
            provider_track_id=f"youtube_music:{video_id}",
            title=str(item.get("title") or "Unavailable track"),
            artists=cls._artists(item.get("artists")),
            album=str(album_name) if isinstance(album_name, str) and album_name else None,
            duration_ms=cls._duration_ms(item.get("duration_seconds") or item.get("duration")),
            occurrence_id=str(set_video_id) if set_video_id else None,
            position=position,
            explicit=explicit,
        )

    @classmethod
    def _requested_title_tokens(cls, value: str) -> tuple[str, ...]:
        """Remove only display-only Spotify suffixes, never requested variants."""

        clean_title = re.sub(r"\s*[-–—]\s*from\b.*$", "", value, flags=re.IGNORECASE)
        clean_title = re.sub(
            r"\s*[-–—]\s*soundtrack\s+version\s*$", "", clean_title, flags=re.IGNORECASE
        )
        return _tokens(clean_title)

    @staticmethod
    def _match_title_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
        return tuple(token for token in tokens if token not in _DISPLAY_TOKENS)

    @classmethod
    def _variant_labels(cls, value: str) -> set[str]:
        tokens = set(_tokens(value))
        return {label for label, markers in _VARIANT_LABELS.items() if markers & tokens}

    @classmethod
    def _title_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        requested_tokens = cls._match_title_tokens(cls._requested_title_tokens(requested.title))
        candidate_tokens = cls._match_title_tokens(_tokens(candidate.title))
        if not requested_tokens or not candidate_tokens:
            return 0.0
        if requested_tokens == candidate_tokens:
            return 55.0
        if len(candidate_tokens) >= len(requested_tokens) and any(
            tuple(candidate_tokens[offset : offset + len(requested_tokens)]) == requested_tokens
            for offset in range(len(candidate_tokens) - len(requested_tokens) + 1)
        ):
            return 52.0
        requested_set = set(requested_tokens)
        overlap = len(requested_set & set(candidate_tokens)) / len(requested_set)
        if overlap >= 0.9:
            return 44.0
        if overlap >= 0.7:
            return 32.0
        return 0.0

    @staticmethod
    def _artist_score(requested: str, candidates: Sequence[str]) -> float:
        requested_tokens = set(_tokens(requested))
        if not requested_tokens:
            return 0.0
        requested_compact = _compact(requested)
        best = 0.0
        for candidate in candidates:
            candidate_tokens = set(_tokens(candidate))
            candidate_compact = _compact(candidate)
            if not candidate_tokens:
                continue
            if requested_tokens == candidate_tokens or requested_compact == candidate_compact:
                best = max(best, 32.0)
            elif requested_tokens <= candidate_tokens or (
                len(requested_compact) >= 4 and requested_compact in candidate_compact
            ):
                best = max(best, 28.0)
            elif len(requested_tokens & candidate_tokens) / len(requested_tokens) >= 0.8:
                best = max(best, 22.0)
        return best

    @classmethod
    def _variant_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        requested_variants = cls._variant_labels(requested.title)
        candidate_variants = cls._variant_labels(candidate.title)
        missing = requested_variants - candidate_variants
        unexpected = candidate_variants - requested_variants
        score = -35.0 * len(missing)
        for label in unexpected:
            # Remasters are frequently the only catalogue result for an older
            # release, but every other alternate recording needs a strong
            # reason to be selected automatically.
            score -= 10.0 if label == "remaster" else 26.0
        return score

    @classmethod
    def _search_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        title_score = cls._title_score(requested, candidate)
        if not title_score:
            return 0.0
        score = title_score
        if requested.artists:
            artist_score = max(
                (cls._artist_score(artist, candidate.artists) for artist in requested.artists),
                default=0.0,
            )
            if not artist_score:
                return 0.0
            score += artist_score
        if requested.duration_ms and candidate.duration_ms:
            difference = abs(requested.duration_ms - candidate.duration_ms)
            if difference <= 2_500:
                score += 12.0
            elif difference <= 8_000:
                score += 6.0
            elif difference > 20_000:
                score -= 20.0
        if (
            requested.album
            and candidate.album
            and _compact(requested.album) == _compact(candidate.album)
        ):
            score += 5.0
        if requested.explicit is not None and candidate.explicit is not None:
            score += 4.0 if requested.explicit == candidate.explicit else -35.0
        return score + cls._variant_score(requested, candidate)

    @classmethod
    def _manual_candidate_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        """Rank plausible alternatives for a deliberate operator choice only."""

        title_score = cls._title_score(requested, candidate)
        if not title_score:
            return 0.0
        score = title_score
        if requested.artists:
            score += max(
                (cls._artist_score(artist, candidate.artists) for artist in requested.artists),
                default=0.0,
            )
        if requested.duration_ms and candidate.duration_ms:
            difference = abs(requested.duration_ms - candidate.duration_ms)
            if difference <= 2_500:
                score += 12.0
            elif difference <= 15_000:
                score += 4.0
            elif difference > 45_000:
                score -= 15.0
        if requested.explicit is not None and candidate.explicit is not None:
            score += 4.0 if requested.explicit == candidate.explicit else -20.0
        # Retain alternate versions in review so a user can intentionally
        # choose a live, remix, cover, or community recording. The cap keeps
        # obviously alternate-but-title-identical recordings visible without
        # ever making them eligible for automatic selection.
        return score + max(cls._variant_score(requested, candidate), -30.0)

    @classmethod
    def _choose_search_candidate(
        cls, requested: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        scored = sorted(
            ((cls._search_score(requested, candidate), candidate) for candidate in candidates),
            key=lambda item: (
                item[0],
                -abs((requested.duration_ms or 0) - (item[1].duration_ms or 0)),
                item[1].provider_track_id,
            ),
            reverse=True,
        )
        if not scored or scored[0][0] < 80.0:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 7.0:
            return None
        return scored[0][1]

    def _search_candidates(self, track: ProviderTrack) -> tuple[ProviderTrack, ...]:
        key = (track.title, track.artists, track.album, track.duration_ms)
        cached = self._search_candidates_cache.get(key)
        if cached is not None:
            return cached
        primary_artist = track.artists[0] if track.artists else ""
        query = " ".join(part for part in (track.title, primary_artist) if part).strip()[:300]
        if not query:
            return ()
        payload = self._call(
            lambda: self._catalog().search(query, filter="songs", limit=YT_MUSIC_SEARCH_LIMIT),
            retry_read=True,
        )
        candidates = tuple(
            candidate
            for item in payload
            if isinstance(item, Mapping)
            if (candidate := self._track(item)) is not None
        )
        self._search_candidates_cache[key] = candidates
        return candidates

    def list_playlists(self) -> Sequence[ProviderPlaylist]:
        playlists = self._call(
            lambda: self._library().get_library_playlists(limit=None), retry_read=True
        )
        return tuple(
            ProviderPlaylist(
                provider_playlist_id=f"youtube_music:{playlist_id}",
                name=str(item.get("title") or "Untitled playlist"),
                description=(
                    str(item["description"])
                    if isinstance(item.get("description"), str) and item.get("description")
                    else None
                ),
                tracks=(),
            )
            for item in playlists
            if isinstance(item, Mapping)
            if isinstance((playlist_id := item.get("playlistId") or item.get("id")), str)
            and playlist_id
        )

    def account_identity(self) -> tuple[str, str]:
        """Return the authenticated YouTube channel handle and display label."""

        payload = self._call(lambda: self._library().get_account_info(), retry_read=True)
        handle = payload.get("channelHandle")
        if not isinstance(handle, str) or not handle.strip():
            raise AuthorizationRequired(
                "YouTube Music did not provide a channel handle; set one and reconnect the account"
            )
        display_name = payload.get("accountName")
        return (
            f"ytmusicapi:handle:{handle.strip().casefold()}",
            str(display_name).strip()
            if isinstance(display_name, str) and display_name.strip()
            else handle,
        )

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = self._raw_id(playlist_id)
        payload = self._call(
            lambda: self._library().get_playlist(raw_id, limit=None), retry_read=True
        )
        tracks: list[ProviderTrack] = []
        raw_tracks = payload.get("tracks")
        if isinstance(raw_tracks, list):
            for position, item in enumerate(raw_tracks):
                if (
                    isinstance(item, Mapping)
                    and (track := self._track(item, position=position)) is not None
                ):
                    tracks.append(track)
        returned_id = payload.get("id")
        return ProviderPlaylist(
            provider_playlist_id=(
                f"youtube_music:{returned_id}"
                if isinstance(returned_id, str) and returned_id
                else f"youtube_music:{raw_id}"
            ),
            name=str(payload.get("title") or "Untitled playlist"),
            description=(
                str(payload["description"])
                if isinstance(payload.get("description"), str) and payload.get("description")
                else None
            ),
            tracks=tuple(tracks),
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
        return self._choose_search_candidate(track, self._search_candidates(track))

    def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
        """Return alternate song recordings for an explicit review-time choice."""

        scored = sorted(
            (
                (self._manual_candidate_score(track, candidate), candidate)
                for candidate in self._search_candidates(track)
            ),
            key=lambda item: (item[0], item[1].provider_track_id),
            reverse=True,
        )
        return tuple(candidate for score, candidate in scored if score >= 25.0)[:5]

    @staticmethod
    def _result_playlist_id(result: str | Mapping[str, Any]) -> str:
        if isinstance(result, str) and result:
            return result
        if isinstance(result, Mapping):
            playlist_id = result.get("playlistId") or result.get("id")
            if isinstance(playlist_id, str) and playlist_id:
                return playlist_id
        raise ProviderUnavailable("YouTube Music did not return a playlist identifier")

    def create_playlist(self, name: str, description: str | None = None) -> ProviderPlaylist:
        result = self._call(
            lambda: self._library().create_playlist(
                name, description or "", privacy_status="PRIVATE"
            ),
        )
        return ProviderPlaylist(
            provider_playlist_id=f"youtube_music:{self._result_playlist_id(result)}",
            name=name,
            description=description or None,
            tracks=(),
        )

    def update_playlist(
        self, playlist_id: str, *, name: str | None = None, description: str | None = None
    ) -> None:
        """Update playlist metadata through ytmusicapi when OPS needs it."""

        if name is None and description is None:
            return
        self._call(
            lambda: self._library().edit_playlist(
                self._raw_id(playlist_id), title=name, description=description
            )
        )

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str | None:
        raw_id = self._raw_id(playlist_id)
        video_ids = [
            self._raw_id(track.provider_track_id)
            for track in tracks
            if track.provider_track_id.startswith("youtube_music:")
        ]
        if len(video_ids) != len(tracks):
            raise TrackUnavailable("the selected recording is not a YouTube Music song")
        for offset in range(0, len(video_ids), _MUTATION_BATCH_SIZE):
            batch = video_ids[offset : offset + _MUTATION_BATCH_SIZE]
            if batch:
                # ytmusicapi otherwise suppresses duplicate video IDs. OPS
                # intentionally preserves duplicate playlist occurrences.
                self._call(
                    lambda batch=batch: self._library().add_playlist_items(
                        raw_id, videoIds=batch, duplicates=True
                    ),
                    track_write=True,
                )
        return None

    def remove_tracks(
        self,
        playlist_id: str,
        tracks: Sequence[ProviderTrack],
        *,
        snapshot_id: str | None = None,
    ) -> str | None:
        del snapshot_id
        if any(not track.occurrence_id for track in tracks):
            raise ValueError("YouTube Music removal requires the playlist occurrence identifier")
        if any(not track.provider_track_id.startswith("youtube_music:") for track in tracks):
            raise TrackUnavailable("the selected recording is not a YouTube Music song")
        raw_id = self._raw_id(playlist_id)
        removals = [
            {
                "videoId": self._raw_id(track.provider_track_id),
                "setVideoId": track.occurrence_id,
            }
            for track in tracks
        ]
        for offset in range(0, len(removals), _MUTATION_BATCH_SIZE):
            batch = removals[offset : offset + _MUTATION_BATCH_SIZE]
            if batch:
                self._call(
                    lambda batch=batch: self._library().remove_playlist_items(raw_id, batch),
                    track_write=True,
                )
        return None

    def reorder_tracks(
        self,
        playlist_id: str,
        track: ProviderTrack,
        *,
        before: ProviderTrack | None = None,
    ) -> None:
        """Move one exact playlist occurrence without conflating duplicates."""

        if not track.occurrence_id or (before is not None and not before.occurrence_id):
            raise ValueError("YouTube Music reordering requires playlist occurrence identifiers")
        if not track.provider_track_id.startswith("youtube_music:") or (
            before is not None and not before.provider_track_id.startswith("youtube_music:")
        ):
            raise TrackUnavailable("the selected recording is not a YouTube Music song")
        move_item: str | tuple[str, str] = track.occurrence_id
        if before is not None:
            move_item = (track.occurrence_id, before.occurrence_id or "")
        self._call(
            lambda: self._library().edit_playlist(self._raw_id(playlist_id), moveItem=move_item)
        )


# Preserve the existing seam for application code while making the specific
# implementation explicit for maintainers and future provider migrations.
YouTubeMusicProvider = YTMusicApiProvider
