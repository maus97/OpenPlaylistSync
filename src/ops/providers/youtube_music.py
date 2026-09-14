"""YouTube Music adapter with supported OAuth-backed library operations.

The rest of OPS only sees provider-neutral values. Public catalogue searches
use ytmusicapi without credentials, while authenticated account and playlist
operations use the supported YouTube Data API v3. Keeping both boundaries in
this module lets the synchronization policy remain provider-neutral.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol, TypeVar

import httpx
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
from ops.providers.errors import NetworkFailure, http_failure
from ops.providers.types import (
    AutomaticCandidateMatch,
    ProviderPlaylist,
    ProviderTrack,
    ScoredCandidate,
)

YT_MUSIC_AUTH_SCHEME = "ytmusicapi_oauth"
YT_MUSIC_SEARCH_LIMIT = 12
_READ_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_MUTATION_BATCH_SIZE = 100
_T = TypeVar("_T")

_DISPLAY_TOKENS = frozenset({"official", "audio", "video", "music", "lyrics", "lyric", "version"})
_TITLE_GROUP = re.compile(r"(\([^()]+\)|\[[^\[\]]+\])")
_FEATURE_CREDIT = re.compile(r"^(?:feat(?:uring)?|ft|with)\.?\s+(.+)$", re.IGNORECASE)
_ARTIST_CREDIT_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bx\b)\s*", re.IGNORECASE)
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

    def get_watch_playlist(
        self,
        videoId: str | None = None,
        playlistId: str | None = None,
        limit: int = 25,
        radio: bool = False,
        shuffle: bool = False,
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


class _YouTubeDataApiClient:
    """Small authenticated client for the supported YouTube Data API.

    YouTube Music's private InnerTube endpoint currently rejects OAuth Bearer
    tokens with HTTP 400.  The Data API accepts the same Google OAuth token
    and exposes the playlist/channel operations OPS needs, so authenticated
    library work is kept on this supported API.  The adapter deliberately
    returns the narrow ytmusicapi-shaped values consumed by the provider
    below; this keeps the matching and sync policy provider-neutral.
    """

    _BASE_URL = "https://www.googleapis.com/youtube/v3"
    _PAGE_SIZE = 50

    def __init__(self, access_token: str, client: httpx.Client | None = None) -> None:
        self._access_token = access_token
        self._token_refresher = None
        self._client = client or httpx.Client(base_url=self._BASE_URL, timeout=20)

    @staticmethod
    def _error_reason(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return None
        error = payload.get("error") if isinstance(payload, Mapping) else None
        errors = error.get("errors", []) if isinstance(error, Mapping) else []
        if not isinstance(errors, list) or not errors:
            return None
        reason = errors[0].get("reason") if isinstance(errors[0], Mapping) else None
        return str(reason) if reason else None

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        renewed = False
        headers = {"Authorization": f"Bearer {self._access_token}"}
        supplied_headers = kwargs.pop("headers", None)
        if isinstance(supplied_headers, Mapping):
            headers.update({str(key): str(value) for key, value in supplied_headers.items()})
        try:
            response = self._client.request(method, endpoint, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkFailure("YouTube Data API could not be reached") from exc
        if response.status_code == 401 and self._token_refresher is not None:
            self._access_token = self._token_refresher(self._access_token)
            renewed = True
            headers["Authorization"] = f"Bearer {self._access_token}"
            try:
                response = self._client.request(method, endpoint, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise NetworkFailure("YouTube Data API could not be reached") from exc
        if response.status_code >= 400:
            reason = self._error_reason(response)
            exc = http_failure(
                response.status_code,
                reason=reason,
                retry=response.headers.get("Retry-After"),
                write=method.upper() not in {"GET", "HEAD"},
            )
            if renewed and response.status_code == 401:
                exc.operation_started_at = datetime.now(UTC)
            raise exc
        # playlistItems.delete succeeds with 204 and no JSON body. Do not turn
        # a completed remote deletion into a failed local journal entry.
        if method.upper() == "DELETE" and response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise YTMusicServerError("YouTube Data API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise YTMusicServerError("YouTube Data API returned an invalid response")
        return payload

    def _paged(self, endpoint: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        expected_total: int | None = None
        for _page in range(1000):
            page_params = {str(key): str(value) for key, value in params.items()}
            page_params["maxResults"] = str(self._PAGE_SIZE)
            if page_token:
                page_params["pageToken"] = page_token
            payload = self._request("GET", endpoint, params=page_params)
            page_items = payload.get("items")
            if not isinstance(page_items, list) or any(
                not isinstance(item, dict) for item in page_items
            ):
                raise ProviderUnavailable("YouTube Music returned an incomplete playlist response")
            page_info = payload.get("pageInfo")
            total = page_info.get("totalResults") if isinstance(page_info, dict) else None
            # Library discovery can advertise inaccessible playlists in its
            # total without returning them (or a next page). Only membership
            # snapshots used for reconciliation require an exact item count.
            if total is not None and endpoint == "/playlistItems":
                if type(total) is not int or total < 0:
                    raise ProviderUnavailable("YouTube Music returned an invalid playlist count")
                if expected_total is not None and total != expected_total:
                    raise ProviderUnavailable(
                        "YouTube Music playlist changed while it was being read"
                    )
                expected_total = total
            items.extend(page_items)
            page_token = payload.get("nextPageToken")
            if page_token is None or page_token == "":  # nosec B105 - pagination, not a credential
                if expected_total is not None and len(items) != expected_total:
                    raise ProviderUnavailable(
                        "YouTube Music returned an incomplete playlist response"
                    )
                return items
            if not isinstance(page_token, str) or not page_items or page_token in seen_tokens:
                raise ProviderUnavailable("YouTube Music returned an invalid playlist page")
            seen_tokens.add(page_token)
        raise ProviderUnavailable("YouTube Music returned too many playlist pages")

    @staticmethod
    def _duration_seconds(value: object) -> float | None:
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
        if not match:
            return None
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return float((hours * 60 + minutes) * 60 + seconds)

    def _video_details(self, video_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        details: dict[str, dict[str, Any]] = {}
        # The same video can occur repeatedly in a playlist. Fetch its metadata
        # once while preserving every playlist occurrence below.
        unique_ids = tuple(dict.fromkeys(video_ids))
        for offset in range(0, len(unique_ids), 50):
            batch = [video_id for video_id in unique_ids[offset : offset + 50] if video_id]
            if not batch:
                continue
            payload = self._request(
                "GET",
                "/videos",
                params={"part": "snippet,contentDetails", "id": ",".join(batch)},
            )
            items = payload.get("items")
            if isinstance(items, list):
                details.update(
                    {
                        str(item["id"]): item
                        for item in items
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    }
                )
        return details

    def get_account_info(self) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/channels",
            params={"part": "id,snippet", "mine": "true", "maxResults": "1"},
        )
        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return {}
        channel = items[0]
        snippet = channel.get("snippet") if isinstance(channel.get("snippet"), dict) else {}
        channel_id = channel.get("id")
        return {
            "channelId": channel_id,
            "channelHandle": snippet.get("customUrl"),
            "accountName": snippet.get("title"),
            "accountPhotoUrl": snippet.get("thumbnails", {}).get("default", {}).get("url")
            if isinstance(snippet.get("thumbnails"), dict)
            else None,
        }

    def get_library_playlists(self, limit: int | None = 25) -> list[dict[str, Any]]:
        del limit
        return [
            {
                "playlistId": item.get("id"),
                "title": (item.get("snippet") or {}).get("title"),
                "description": (item.get("snippet") or {}).get("description"),
            }
            for item in self._paged("/playlists", {"part": "snippet", "mine": "true"})
            if item.get("id")
        ]

    def get_playlist(
        self,
        playlistId: str,
        limit: int | None = 100,
        related: bool = False,
        suggestions_limit: int = 0,
    ) -> dict[str, Any]:
        del limit, related, suggestions_limit
        playlist_payload = self._request(
            "GET",
            "/playlists",
            params={"part": "snippet,contentDetails", "id": playlistId, "maxResults": "1"},
        )
        playlist_items = playlist_payload.get("items")
        if not isinstance(playlist_items, list) or not playlist_items:
            raise ProviderUnavailable(
                "YouTube Music could not access this playlist; it may be deleted or private"
            )
        playlist = playlist_items[0]
        if not isinstance(playlist, dict) or playlist.get("id") != playlistId:
            raise ProviderUnavailable("YouTube Music returned an invalid playlist response")
        raw_items = self._paged(
            "/playlistItems", {"part": "snippet,contentDetails", "playlistId": playlistId}
        )
        content_details = playlist.get("contentDetails")
        item_count = content_details.get("itemCount") if isinstance(content_details, dict) else None
        if item_count is not None and (type(item_count) is not int or item_count != len(raw_items)):
            raise ProviderUnavailable("YouTube Music returned an incomplete playlist response")
        video_ids = [
            str((item.get("contentDetails") or {}).get("videoId"))
            for item in raw_items
            if isinstance(item.get("contentDetails"), dict)
            and (item.get("contentDetails") or {}).get("videoId")
        ]
        videos = self._video_details(video_ids)
        tracks: list[dict[str, Any]] = []
        for position, item in enumerate(raw_items):
            content = (
                item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
            )
            video_id = content.get("videoId")
            if not isinstance(video_id, str) or not video_id:
                raise ProviderUnavailable("YouTube Music returned an unreadable playlist entry")
            if not isinstance(item.get("id"), str) or not item["id"]:
                raise ProviderUnavailable("YouTube Music returned an unidentified playlist entry")
            details = videos.get(video_id, {})
            video_snippet = (
                details.get("snippet") if isinstance(details.get("snippet"), dict) else {}
            )
            item_snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
            channel = video_snippet.get("channelTitle") or item_snippet.get(
                "videoOwnerChannelTitle"
            )
            duration = (details.get("contentDetails") or {}).get("duration")
            tracks.append(
                {
                    "videoId": video_id,
                    "setVideoId": item.get("id"),
                    "title": video_snippet.get("title") or item_snippet.get("title"),
                    "artists": (
                        [{"name": channel}] if isinstance(channel, str) and channel else []
                    ),
                    "duration_seconds": self._duration_seconds(duration),
                    "position": item_snippet.get("position", position),
                }
            )
        snippet = playlist.get("snippet") if isinstance(playlist.get("snippet"), dict) else {}
        return {
            "id": playlist.get("id") or playlistId,
            "title": snippet.get("title") or "Untitled playlist",
            "description": snippet.get("description"),
            "tracks": tracks,
        }

    def create_playlist(
        self,
        title: str,
        description: str,
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
        source_playlist: str | None = None,
    ) -> str:
        del video_ids, source_playlist
        payload = self._request(
            "POST",
            "/playlists",
            params={"part": "snippet,status"},
            headers={"Content-Type": "application/json"},
            json={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": privacy_status.casefold()},
            },
        )
        playlist_id = payload.get("id")
        if not isinstance(playlist_id, str) or not playlist_id:
            raise YTMusicServerError("YouTube Data API did not return a playlist identifier")
        return playlist_id

    def add_playlist_items(
        self,
        playlistId: str,
        videoIds: list[str] | None = None,
        source_playlist: str | None = None,
        duplicates: bool = False,
    ) -> str:
        del source_playlist, duplicates
        for video_id in videoIds or []:
            self._request(
                "POST",
                "/playlistItems",
                params={"part": "snippet"},
                headers={"Content-Type": "application/json"},
                json={
                    "snippet": {
                        "playlistId": playlistId,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            )
        return "ok"

    def remove_playlist_items(self, playlistId: str, videos: list[dict[str, Any]]) -> str:
        del playlistId
        for video in videos:
            occurrence_id = video.get("setVideoId")
            if isinstance(occurrence_id, str) and occurrence_id:
                self._request("DELETE", "/playlistItems", params={"id": occurrence_id})
        return "ok"

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
    ) -> str:
        del privacyStatus, collaboration, addPlaylistId, sortOrder, addToTop, voteOption
        if title is not None or description is not None:
            current = self._request(
                "GET",
                "/playlists",
                params={"part": "snippet", "id": playlistId, "maxResults": "1"},
            )
            items = current.get("items")
            existing = (
                items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
            )
            snippet = existing.get("snippet") if isinstance(existing.get("snippet"), dict) else {}
            self._request(
                "PUT",
                "/playlists",
                params={"part": "snippet"},
                headers={"Content-Type": "application/json"},
                json={
                    "id": playlistId,
                    "snippet": {
                        "title": (
                            title
                            if title is not None
                            else snippet.get("title", "Untitled playlist")
                        ),
                        "description": description
                        if description is not None
                        else snippet.get("description", ""),
                    },
                },
            )
        if moveItem is not None:
            current_id = moveItem[0] if isinstance(moveItem, tuple) else moveItem
            before_id = moveItem[1] if isinstance(moveItem, tuple) else None
            items = self._paged(
                "/playlistItems", {"part": "snippet,contentDetails", "playlistId": playlistId}
            )
            current = next((item for item in items if item.get("id") == current_id), None)
            before = next((item for item in items if item.get("id") == before_id), None)
            if isinstance(current, dict):
                current_snippet = current.get("snippet")
                current_snippet = current_snippet if isinstance(current_snippet, dict) else {}
                position = (
                    (before.get("snippet") or {}).get("position")
                    if isinstance(before, dict)
                    else len(items) - 1
                )
                resource = current_snippet.get("resourceId")
                if not isinstance(resource, dict):
                    video_id = (current.get("contentDetails") or {}).get("videoId")
                    resource = {"kind": "youtube#video", "videoId": video_id}
                self._request(
                    "PUT",
                    "/playlistItems",
                    params={"part": "snippet"},
                    headers={"Content-Type": "application/json"},
                    json={
                        "id": current_id,
                        "snippet": {
                            "playlistId": playlistId,
                            "resourceId": resource,
                            "position": position,
                        },
                    },
                )
        return "ok"


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z0-9]+", normalized.casefold()))


def _compact(value: str) -> str:
    return "".join(_tokens(value))


class YTMusicApiProvider:
    """YouTube Music provider contract with a supported authenticated path.

    Public catalogue searches intentionally use an unauthenticated ytmusicapi
    client. Authenticated playlist and account operations use the official
    YouTube Data API adapter because YouTube Music's private InnerTube endpoint
    rejects OAuth Bearer tokens with HTTP 400.
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
        http_client: httpx.Client | None = None,
        oauth_credentials: OAuthCredentials | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = dict(credentials or {})
        self._client_id = client_id
        self._client_secret = client_secret
        self._library_client = library_client
        self._catalog_client = catalog_client
        self._http_client = http_client
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
                "YouTube Music needs to be reconnected for the current OAuth integration"
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
            token = self._oauth_token()
            self._library_client = _YouTubeDataApiClient(
                str(token["access_token"]), client=self._http_client
            )
            self._library_client._token_refresher = getattr(self, "_token_refresher", None)
        except (YTMusicError, ValueError, TypeError, KeyError) as exc:
            raise AuthorizationRequired("YouTube Music account needs to be reconnected") from exc
        return self._library_client

    def set_token_refresher(self, refresher) -> None:
        self._token_refresher = refresher
        if isinstance(self._library_client, _YouTubeDataApiClient):
            self._library_client._token_refresher = refresher

    def _catalog(self) -> YTMusicClient:
        if self._catalog_client is None:
            try:
                self._catalog_client = YTMusic()
            except (YTMusicError, ValueError, TypeError) as exc:
                raise ProviderUnavailable("YouTube Music search is unavailable") from exc
        return self._catalog_client

    @staticmethod
    def _status_code(exc: BaseException) -> int | None:
        if isinstance(exc, requests.RequestException) and exc.response is not None:
            return exc.response.status_code
        match = _STATUS_CODE.search(str(exc))
        return int(match.group(1)) if match else None

    @classmethod
    def _retryable(cls, exc: BaseException) -> bool:
        if isinstance(exc, requests.RequestException):
            return exc.response is None or exc.response.status_code >= 500
        return isinstance(exc, YTMusicServerError) and (cls._status_code(exc) or 0) >= 500

    @classmethod
    def _raise_provider_error(cls, exc: BaseException, *, track_write: bool) -> None:
        if isinstance(exc, requests.RequestException):
            if exc.response is None:
                raise NetworkFailure("YouTube Music could not be reached") from exc
            raise http_failure(
                exc.response.status_code,
                retry=exc.response.headers.get("Retry-After"),
                write=track_write,
            ) from exc
        status_code = cls._status_code(exc)
        text = str(exc).casefold()
        if (
            status_code == 429
            or "rate limit" in text
            or "too many requests" in text
            or "quota" in text
        ):
            raise RateLimited() from exc
        if status_code == 403:
            raise http_failure(403, write=track_write) from exc
        if status_code == 401:
            raise AuthorizationRequired(
                "YouTube Music authorization expired; reconnect the account"
            ) from exc
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
    def _title_metadata(
        cls, value: str, known_artists: Sequence[str] = ()
    ) -> tuple[str, tuple[str, ...]]:
        """Separate a recording title from display-only performer credits.

        Providers commonly disagree about whether a featured performer belongs
        in the title or the artist list.  Treating those credits as title words
        caused exact recordings such as ``Perfect Duet (feat. Beyoncé)`` to be
        missed.  Version labels (acoustic, live, remix, and so on) deliberately
        remain part of the title so they continue to protect recording intent.
        """

        known_keys = {_compact(artist) for artist in known_artists if _compact(artist)}
        credits: list[str] = []

        def replace_group(match: re.Match[str]) -> str:
            content = match.group(0)[1:-1].strip()
            feature_match = _FEATURE_CREDIT.fullmatch(content)
            if feature_match:
                credits.extend(
                    part for part in _ARTIST_CREDIT_SPLIT.split(feature_match.group(1)) if part
                )
                return ""

            lowered = content.casefold()
            if lowered.startswith("from ") or "soundtrack version" in lowered:
                return ""

            parts = tuple(part for part in _ARTIST_CREDIT_SPLIT.split(content) if part)
            if parts and known_keys and all(_compact(part) in known_keys for part in parts):
                credits.extend(parts)
                return ""
            return match.group(0)

        clean_title = _TITLE_GROUP.sub(replace_group, value)
        clean_title = re.sub(r"\s*[-–—]\s*from\b.*$", "", clean_title, flags=re.IGNORECASE)
        clean_title = re.sub(
            r"\s*[-–—]\s*soundtrack\s+version\s*$", "", clean_title, flags=re.IGNORECASE
        )
        clean_title = re.sub(r"\s+", " ", clean_title).strip(" -–—")

        unique_credits: list[str] = []
        seen: set[str] = set()
        for credit in credits:
            key = _compact(credit)
            if key and key not in seen:
                seen.add(key)
                unique_credits.append(credit.strip())
        return clean_title, tuple(unique_credits)

    @classmethod
    def _requested_title_tokens(cls, value: str, artists: Sequence[str] = ()) -> tuple[str, ...]:
        """Remove display metadata and redundant credits, but retain variants."""

        clean_title, _credits = cls._title_metadata(value, artists)
        return _tokens(clean_title)

    @staticmethod
    def _match_title_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
        return tuple(token for token in tokens if token not in _DISPLAY_TOKENS)

    @classmethod
    def _variant_labels(cls, value: str) -> set[str]:
        tokens = set(_tokens(value))
        labels = {label for label, markers in _VARIANT_LABELS.items() if markers & tokens}
        if re.search(r"\b(?:originally performed by|in the style of)\b", value, re.IGNORECASE):
            labels.add("cover")
        return labels

    @classmethod
    def _effective_artists(cls, track: ProviderTrack) -> tuple[str, ...]:
        _title, credits = cls._title_metadata(track.title, track.artists)
        artists: list[str] = []
        seen: set[str] = set()
        for artist in (*track.artists, *credits):
            key = _compact(artist)
            if key and key not in seen:
                seen.add(key)
                artists.append(artist)
        return tuple(artists)

    @classmethod
    def _title_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        requested_tokens = cls._match_title_tokens(
            cls._requested_title_tokens(requested.title, requested.artists)
        )
        candidate_title, _credits = cls._title_metadata(candidate.title, candidate.artists)
        candidate_tokens = cls._match_title_tokens(_tokens(candidate_title))
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
    def _artist_coverage_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        """Reward corroboration from multiple provider artist credits."""

        requested_artists = cls._effective_artists(requested)
        candidate_artists = cls._effective_artists(candidate)
        scores = [cls._artist_score(artist, candidate_artists) for artist in requested_artists]
        matching_scores = [score for score in scores if score]
        if not matching_scores:
            return 0.0
        return max(matching_scores) + min(14.0, 7.0 * (len(matching_scores) - 1))

    @classmethod
    def _unexpected_credit_penalty(
        cls, requested: ProviderTrack, candidate: ProviderTrack
    ) -> float:
        """Penalize a candidate that introduces a different featured performer."""

        _title, candidate_credits = cls._title_metadata(candidate.title, candidate.artists)
        if not candidate_credits:
            return 0.0
        requested_artists = cls._effective_artists(requested)
        unexpected = sum(
            1 for credit in candidate_credits if not cls._artist_score(credit, requested_artists)
        )
        return -18.0 * unexpected

    @classmethod
    def _recording_key(cls, track: ProviderTrack) -> tuple[object, ...]:
        """Group duplicate search cards that describe the same recording."""

        title, _credits = cls._title_metadata(track.title, track.artists)
        artist_keys = tuple(sorted(_compact(artist) for artist in cls._effective_artists(track)))
        duration_bucket = round(track.duration_ms / 2_000) if track.duration_ms else None
        return (
            _compact(title),
            artist_keys,
            tuple(sorted(cls._variant_labels(track.title))),
            duration_bucket,
            track.explicit,
        )

    @classmethod
    def _ranked_candidates(
        cls,
        requested: ProviderTrack,
        candidates: Sequence[ProviderTrack],
        *,
        manual: bool = False,
    ) -> list[tuple[float, ProviderTrack]]:
        scorer = cls._manual_candidate_score if manual else cls._search_score
        requested_surface = cls._match_title_tokens(_tokens(requested.title))

        def rank(item: tuple[float, ProviderTrack]) -> tuple[float, bool, int, str]:
            score, candidate = item
            candidate_surface = cls._match_title_tokens(_tokens(candidate.title))
            duration_difference = abs((requested.duration_ms or 0) - (candidate.duration_ms or 0))
            return (
                score,
                candidate_surface == requested_surface,
                -duration_difference,
                candidate.provider_track_id,
            )

        by_recording: dict[tuple[object, ...], tuple[float, ProviderTrack]] = {}
        for candidate in candidates:
            item = (scorer(requested, candidate), candidate)
            key = cls._recording_key(candidate)
            previous = by_recording.get(key)
            if previous is None or rank(item) > rank(previous):
                by_recording[key] = item
        return sorted(by_recording.values(), key=rank, reverse=True)

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
            artist_score = cls._artist_coverage_score(requested, candidate)
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
        return (
            score
            + cls._variant_score(requested, candidate)
            + cls._unexpected_credit_penalty(requested, candidate)
        )

    @classmethod
    def _manual_candidate_score(cls, requested: ProviderTrack, candidate: ProviderTrack) -> float:
        """Rank plausible alternatives for a deliberate operator choice only."""

        title_score = cls._title_score(requested, candidate)
        if not title_score:
            return 0.0
        score = title_score
        if requested.artists:
            score += cls._artist_coverage_score(requested, candidate)
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
        return (
            score
            + max(cls._variant_score(requested, candidate), -30.0)
            + cls._unexpected_credit_penalty(requested, candidate)
        )

    @classmethod
    def _choose_search_candidate(
        cls, requested: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        scored = cls._ranked_candidates(requested, candidates)
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
        base_title, _credits = self._title_metadata(track.title, track.artists)
        query = " ".join(part for part in (base_title, primary_artist) if part).strip()[:300]
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
        """Return a stable account identity and display label.

        ``channelId`` is the stable identifier returned by the official Data
        API. ``channelHandle`` remains the strongest identifier exposed by the
        legacy injected ytmusicapi client, but YouTube does not return one for
        every account. In that case use a
        privacy-preserving digest of the account name and avatar URL rather
        than rejecting an otherwise valid connection or storing profile data as
        the account key. If the account menu is incomplete, bind the connection
        to a one-way refresh-token fingerprint as a last resort. A new token
        then requires an explicit reconnect, which is safer than guessing that
        two incomplete profiles are the same account.
        """

        try:
            payload = self._call(lambda: self._library().get_account_info(), retry_read=True)
        except (KeyError, IndexError):
            # Keep the legacy injected client seam usable when its optional
            # account-menu parser cannot produce a profile identity.
            return self._token_fallback_identity()
        if not isinstance(payload, Mapping):
            return self._token_fallback_identity()
        channel_id = payload.get("channelId")
        if isinstance(channel_id, str) and channel_id.strip():
            display_name = payload.get("accountName")
            return (
                channel_id.strip(),
                str(display_name).strip()
                if isinstance(display_name, str) and display_name.strip()
                else "YouTube Music",
            )
        handle = payload.get("channelHandle")
        if not isinstance(handle, str) or not handle.strip():
            account_name = payload.get("accountName")
            photo_url = payload.get("accountPhotoUrl")
            if not isinstance(account_name, str) or not account_name.strip():
                return self._token_fallback_identity()
            if not isinstance(photo_url, str) or not photo_url.strip():
                return self._token_fallback_identity(account_name)
            identity_material = "\x1f".join(
                (
                    unicodedata.normalize("NFKC", account_name).strip().casefold(),
                    photo_url.strip(),
                )
            )
            digest = sha256(identity_material.encode("utf-8")).hexdigest()[:32]
            return f"ytmusicapi:account:{digest}", account_name.strip()
        display_name = payload.get("accountName")
        return (
            f"ytmusicapi:handle:{handle.strip().casefold()}",
            str(display_name).strip()
            if isinstance(display_name, str) and display_name.strip()
            else handle,
        )

    def _token_fallback_identity(self, account_name: object | None = None) -> tuple[str, str]:
        """Bind incomplete account metadata to a non-reversible token digest."""

        refresh_token = self._credentials.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise AuthorizationRequired("YouTube Music did not provide an account identity")
        digest = sha256(
            ("ytmusicapi-token-identity-v1\x1f" + refresh_token).encode("utf-8")
        ).hexdigest()[:32]
        display_name = (
            account_name.strip()
            if isinstance(account_name, str) and account_name.strip()
            else "YouTube Music account"
        )
        return f"ytmusicapi:token:{digest}", display_name

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        raw_id = self._raw_id(playlist_id)
        payload = self._call(
            lambda: self._library().get_playlist(raw_id, limit=None), retry_read=True
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("tracks"), list):
            raise ProviderUnavailable("YouTube Music returned an incomplete playlist response")
        tracks: list[ProviderTrack] = []
        for position, item in enumerate(payload["tracks"]):
            if (
                not isinstance(item, Mapping)
                or (track := self._track(item, position=position)) is None
            ):
                raise ProviderUnavailable("YouTube Music returned an unreadable playlist entry")
            tracks.append(track)
        returned_id = payload.get("id")
        if returned_id is not None and returned_id != raw_id:
            raise ProviderUnavailable("YouTube Music returned a different playlist")
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

    @classmethod
    def resolve_search_candidates(
        cls, track: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> ProviderTrack | None:
        """Re-score an existing result set after source metadata was enriched."""

        return cls._choose_search_candidate(track, candidates)

    @classmethod
    def best_available_match(
        cls, track: ProviderTrack, candidates: Sequence[ProviderTrack]
    ) -> AutomaticCandidateMatch | None:
        """Choose the strongest plausible result when strict ranking is ambiguous."""

        ranked = tuple(
            ScoredCandidate(candidate, score)
            for score, candidate in cls._ranked_candidates(track, candidates)
            # A zero score means title or artist identity failed. Variant and
            # duration penalties may lower a still-plausible recording, but
            # must not turn unrelated metadata into an automatic match.
            if score >= 25.0
        )
        if not ranked:
            return None
        return AutomaticCandidateMatch(
            selected=ranked[0].track,
            score=ranked[0].score,
            alternatives=ranked[1:5],
            reason=(
                "top candidates had similar YouTube Music matching scores"
                if len(ranked) > 1 and ranked[0].score - ranked[1].score < 7.0
                else (
                    "the best viable YouTube Music result was below the strict confidence threshold"
                )
            ),
        )

    def enrich_track_metadata(self, track: ProviderTrack) -> ProviderTrack:
        """Fetch release metadata only when a normal cross-provider match is ambiguous.

        The official YouTube Data API exposes playlist membership and duration
        but not the YouTube Music album. A single public watch-playlist lookup
        can supply that release evidence. The coordinator calls this fallback
        only after the destination's ordinary search is unresolved, avoiding a
        per-track request for the common path.
        """

        if not track.provider_track_id.startswith("youtube_music:"):
            return track
        raw_id = self._raw_id(track.provider_track_id)
        payload = self._call(
            lambda: self._catalog().get_watch_playlist(videoId=raw_id, limit=1),
            retry_read=True,
        )
        raw_tracks = payload.get("tracks") if isinstance(payload, Mapping) else None
        if not isinstance(raw_tracks, list):
            return track
        enriched: ProviderTrack | None = None
        for item in raw_tracks:
            if not isinstance(item, Mapping) or item.get("videoId") != raw_id:
                continue
            enriched = self._track(item)
            break
        if enriched is None:
            return track
        return ProviderTrack(
            provider_track_id=track.provider_track_id,
            title=enriched.title or track.title,
            artists=enriched.artists or track.artists,
            album=track.album or enriched.album,
            duration_ms=track.duration_ms or enriched.duration_ms,
            isrc=track.isrc or enriched.isrc,
            occurrence_id=track.occurrence_id,
            position=track.position,
            explicit=track.explicit if track.explicit is not None else enriched.explicit,
        )

    def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
        """Return alternate song recordings for an explicit review-time choice."""

        scored = self._ranked_candidates(track, self._search_candidates(track), manual=True)
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
