"""Persistence-aware review, one-time approval, and recoverable execution."""

import hashlib
import json
import secrets
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import (
    YOUTUBE_MUSIC_AUTH_SCHEME,
    YouTubeMusicAuthService,
    oauth_token_needs_refresh,
)
from ops.config import Settings
from ops.models import (
    ProviderAccount,
    ProviderSearchCache,
    ProviderTrackMapping,
    SyncAction,
    SyncBaseline,
    SyncPair,
    SyncRun,
)
from ops.providers.base import AuthorizationRequired, ProviderError
from ops.providers.types import ProviderTrack
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncActionRepository,
    SyncBaselineRepository,
    SyncRunRepository,
)
from ops.sync.domain import (
    TRACK_IDENTITY_VERSION,
    ActionType,
    BaselineState,
    InitialSyncPolicy,
    PlaylistState,
    ReconciliationAction,
    ReconciliationPlan,
    Side,
    SyncMode,
    TrackState,
    reconcile,
)
from ops.sync.executor import PlanExecutionError, SyncExecutor, SyncProvider
from ops.sync.leases import PairOperationBusy, acquire_pair_lease
from ops.sync.safety import Approval, DestructiveActionApprovalError, plan_fingerprint
from ops.sync.serialization import (
    decode_baseline,
    decode_plan,
    encode_baseline,
    encode_plan,
    playlist_state_hash,
)

ProviderFactory = Callable[[ProviderAccount, dict[str, Any]], SyncProvider]
REVIEW_APPROVAL_TTL = timedelta(minutes=15)
REVIEW_REUSE_WINDOW = timedelta(minutes=2)
MAX_PLAYLIST_TRACKS = 10_000
MAX_PLAN_ACTIONS = 5_000
MAX_REVIEW_LOOKUPS = 500
MAX_MANUAL_CANDIDATES = 5
# Versioned because this cache stores matching decisions.  Bump whenever the
# matching semantics change so old false negatives do not suppress a repaired
# review for twelve hours.
SEARCH_CACHE_ALGORITHM_VERSION = 6
RESOLVED_SEARCH_CACHE_TTL = timedelta(days=14)
UNRESOLVED_SEARCH_CACHE_TTL = timedelta(hours=12)


class ReviewNotApplicable(ValueError):
    """Raised when a review does not belong to this pair or cannot be applied."""


class ReviewExpired(ValueError):
    """Raised when an operator approval outlives its short validity window."""


class TrackMappingConflict(PlanExecutionError):
    """Raised instead of silently overwriting a verified pair-scoped identity."""


class PartialSyncRecoveryRequired(PlanExecutionError):
    """Raised when a failed first sync left provider occurrence counts uneven."""


class AmbiguousSpotifyRemoval(PlanExecutionError):
    """Raised when Spotify cannot target the reviewed duplicate occurrence safely."""


@dataclass(frozen=True, slots=True)
class PreparedReview:
    review_id: int
    plan: ReconciliationPlan
    unresolved_actions: tuple[ReconciliationAction, ...]
    approval_token: str
    status: str
    approval_expires_at: datetime | None
    candidate_options: tuple["ManualCandidateOptions", ...] = ()
    source_track_count: int | None = None
    target_track_count: int | None = None
    source_name: str | None = None
    target_name: str | None = None
    replacement: bool = False
    replacement_track: ProviderTrack | None = None

    @property
    def baseline_upgrade_required(self) -> bool:
        return self.status == "baseline_upgrade"


@dataclass(frozen=True, slots=True)
class ManualCandidateOptions:
    """Bounded, persisted fallback choices that require an operator decision."""

    action_index: int
    action: ReconciliationAction
    candidates: tuple[ProviderTrack, ...]


@dataclass(frozen=True, slots=True)
class EquivalentTrackPair:
    """Existing source/target items proven equivalent by provider search."""

    source_track: ProviderTrack
    target_track: ProviderTrack
    canonical_key: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provider_track_dict(track: ProviderTrack) -> dict[str, object]:
    return {
        "provider_track_id": track.provider_track_id,
        "title": track.title,
        "artists": list(track.artists),
        "album": track.album,
        "duration_ms": track.duration_ms,
        "isrc": track.isrc,
        "occurrence_id": track.occurrence_id,
        "position": track.position,
        "explicit": track.explicit,
    }


def _provider_track_from_dict(payload: dict[str, object]) -> ProviderTrack:
    return ProviderTrack(
        provider_track_id=str(payload["provider_track_id"]),
        title=str(payload["title"]),
        artists=tuple(str(artist) for artist in payload["artists"]),
        album=str(payload["album"]) if payload.get("album") else None,
        duration_ms=payload.get("duration_ms"),
        isrc=str(payload["isrc"]) if payload.get("isrc") else None,
        occurrence_id=(str(payload["occurrence_id"]) if payload.get("occurrence_id") else None),
        position=payload.get("position"),
        explicit=payload.get("explicit") if isinstance(payload.get("explicit"), bool) else None,
    )


def _encode_resolutions(resolutions: dict[int, ProviderTrack]) -> str:
    return json.dumps(
        {str(index): _provider_track_dict(track) for index, track in resolutions.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_resolutions(value: str | None) -> dict[int, ProviderTrack]:
    if not value:
        return {}
    payload = json.loads(value)
    return {
        int(index): _provider_track_from_dict(track_payload)
        for index, track_payload in payload.items()
    }


def _encode_candidates(candidates: dict[int, tuple[ProviderTrack, ...]]) -> str | None:
    if not candidates:
        return None
    return json.dumps(
        {
            str(index): [_provider_track_dict(track) for track in tracks]
            for index, tracks in candidates.items()
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_candidates(value: str | None) -> dict[int, tuple[ProviderTrack, ...]]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return {
            int(index): tuple(_provider_track_from_dict(track) for track in tracks)
            for index, tracks in payload.items()
            if isinstance(tracks, list)
        }
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return {}


def _action_provider_track(action: ReconciliationAction) -> ProviderTrack:
    """Restore full resolver evidence from one persisted plan action."""

    return ProviderTrack(
        provider_track_id=action.track.source_provider_track_id,
        title=action.track.title,
        artists=action.track.artists,
        album=action.track.album,
        duration_ms=action.track.duration_ms,
        isrc=action.track.isrc,
        occurrence_id=action.track.occurrence_id,
        position=action.track.position,
        explicit=action.track.explicit,
    )


def _state_provider_track(track: TrackState) -> ProviderTrack:
    """Restore provider evidence from a current playlist occurrence."""

    return ProviderTrack(
        provider_track_id=track.source_provider_track_id,
        title=track.title,
        artists=track.artists,
        album=track.album,
        duration_ms=track.duration_ms,
        isrc=track.isrc,
        occurrence_id=track.occurrence_id,
        position=track.position,
        explicit=track.explicit,
    )


def _search_fingerprint(track: ProviderTrack, *, explicit_preference: str = "no_preference") -> str:
    """Hash only normalized matching evidence; never persist raw credentials."""

    payload = json.dumps(
        {
            "provider_track_id": track.provider_track_id,
            "title": track.title,
            "artists": track.artists,
            "album": track.album,
            "duration_ms": track.duration_ms,
            "isrc": track.isrc.casefold() if track.isrc else None,
            "explicit": track.explicit,
            "explicit_preference": explicit_preference,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_isrc(value: str | None) -> str | None:
    return value.strip().casefold() if value and value.strip() else None


class SyncCoordinator:
    """Create bounded reviews and apply each exact approved state at most once."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        provider_factory: ProviderFactory,
    ) -> None:
        self.session = session
        self.settings = settings
        self.provider_factory = provider_factory
        self.cipher = (
            CredentialCipher(settings.credential_encryption_key)
            if settings.credential_encryption_key
            else None
        )

    def _search_cache_fingerprint(self, track: ProviderTrack) -> str:
        """Keep cached automatic choices scoped to the selected content preference."""

        return _search_fingerprint(track, explicit_preference=self.settings.explicit_preference)

    def _resolve_explicit_preference(
        self,
        provider: SyncProvider,
        track: ProviderTrack,
        candidates: Sequence[ProviderTrack],
    ) -> ProviderTrack | None:
        resolver = getattr(provider, "resolve_explicit_preference", None)
        if not callable(resolver) or not candidates:
            return None
        return resolver(track, candidates, self.settings.explicit_preference)

    def _refresh_spotify_credentials(
        self, account: ProviderAccount, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        expires_at = credentials.get("expires_at")
        if not expires_at:
            return credentials
        try:
            expiry = datetime.fromisoformat(str(expires_at)).astimezone(UTC)
        except ValueError:
            return credentials
        if expiry > datetime.now(UTC):
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise ValueError("Spotify authorization expired; reconnect the account")
        if not self.settings.spotify_client_id or not self.settings.spotify_client_secret:
            raise ValueError("Spotify OAuth settings are incomplete")
        refreshed = SpotifyOAuthService(
            SpotifyOAuthConfig(
                self.settings.spotify_client_id,
                self.settings.spotify_client_secret,
                self.settings.spotify_redirect_uri,
            )
        ).refresh_token(str(refresh_token))
        merged = {
            **credentials,
            **refreshed,
            "refresh_token": refreshed.get("refresh_token", refresh_token),
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            ).isoformat(),
        }
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        ProviderAccountRepository(self.session, self.cipher).save_credentials(account, merged)
        self.session.commit()
        return merged

    def _refresh_youtube_music_credentials(
        self, account: ProviderAccount, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        if credentials.get("auth_scheme") != YOUTUBE_MUSIC_AUTH_SCHEME:
            raise ValueError(
                "YouTube Music needs to be reconnected for the current OAuth integration"
            )
        if not oauth_token_needs_refresh(credentials):
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise ValueError("YouTube Music authorization expired; reconnect the account")
        if not self.settings.ytmusic_client_id or not self.settings.ytmusic_client_secret:
            raise ValueError("YouTube Music OAuth settings are incomplete")
        refreshed = YouTubeMusicAuthService(
            self.settings.ytmusic_client_id, self.settings.ytmusic_client_secret
        ).refresh_token(str(refresh_token))
        merged = {
            **credentials,
            **refreshed,
            "refresh_token": refreshed.get("refresh_token", refresh_token),
        }
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        ProviderAccountRepository(self.session, self.cipher).save_credentials(account, merged)
        self.session.commit()
        return merged

    def _credentials(self, account: ProviderAccount) -> dict[str, Any]:
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        credentials = ProviderAccountRepository(self.session, self.cipher).load_credentials(account)
        if account.provider_name == "spotify":
            credentials = self._refresh_spotify_credentials(account, credentials)
        elif account.provider_name == "youtube_music":
            credentials = self._refresh_youtube_music_credentials(account, credentials)
            if not self.settings.ytmusic_client_id or not self.settings.ytmusic_client_secret:
                raise ValueError("YouTube Music OAuth settings are incomplete")
            credentials = {
                **credentials,
                "_ytmusic_client_id": self.settings.ytmusic_client_id,
                "_ytmusic_client_secret": self.settings.ytmusic_client_secret,
            }
        return credentials

    def _providers(
        self, pair: SyncPair
    ) -> tuple[SyncProvider, SyncProvider, ProviderAccount, ProviderAccount]:
        source_account = self.session.get(ProviderAccount, pair.source_account_id)
        target_account = self.session.get(ProviderAccount, pair.target_account_id)
        if source_account is None or target_account is None:
            raise ValueError("sync pair references a missing provider account")
        source_provider = self.provider_factory(source_account, self._credentials(source_account))
        target_provider = self.provider_factory(target_account, self._credentials(target_account))
        return source_provider, target_provider, source_account, target_account

    @staticmethod
    def _policy(pair: SyncPair) -> InitialSyncPolicy:
        if SyncCoordinator._mode(pair) is SyncMode.SOURCE_TO_TARGET:
            return InitialSyncPolicy.SOURCE_AUTHORITATIVE
        try:
            return InitialSyncPolicy(pair.initial_sync_policy)
        except ValueError:
            return InitialSyncPolicy.MERGE

    @staticmethod
    def _mode(pair: SyncPair) -> SyncMode:
        try:
            return SyncMode(pair.sync_mode)
        except ValueError:
            return SyncMode.TWO_WAY

    @staticmethod
    def _pair_binding(pair: SyncPair) -> str:
        """Bind a review to the configuration the operator actually reviewed."""
        values = (
            pair.id,
            str(pair.created_at),
            pair.source_account_id,
            pair.target_account_id,
            pair.source_playlist_id,
            pair.target_playlist_id,
            pair.sync_mode,
            pair.initial_sync_policy,
        )
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def _review_context_matches(self, pair: SyncPair, run: SyncRun) -> bool:
        try:
            summary = json.loads(run.summary_json or "{}")
        except (ValueError, TypeError):
            return False
        baseline = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        return (
            isinstance(summary, dict)
            and summary.get("pair_binding") == self._pair_binding(pair)
            and run.baseline_id == (baseline.id if baseline else None)
        )

    def _invalidate_reviews(self, pair_id: int) -> None:
        self.session.execute(
            update(SyncRun)
            .where(
                SyncRun.pair_id == pair_id,
                SyncRun.status.in_(("planned", "conflict", "baseline_upgrade")),
            )
            .values(status="stale", approval_token_hash=None)
        )

    def _current_state(
        self, pair: SyncPair
    ) -> tuple[PlaylistState, PlaylistState, SyncProvider, SyncProvider]:
        source_provider, target_provider, source_account, target_account = self._providers(pair)
        source = self._apply_track_mappings(
            pair.id,
            source_account.id,
            PlaylistState.from_provider_playlist(
                source_provider.get_playlist(pair.source_playlist_id)
            ),
        )
        target = self._apply_track_mappings(
            pair.id,
            target_account.id,
            PlaylistState.from_provider_playlist(
                target_provider.get_playlist(pair.target_playlist_id)
            ),
        )
        if len(source.tracks) > MAX_PLAYLIST_TRACKS or len(target.tracks) > MAX_PLAYLIST_TRACKS:
            raise ValueError(
                f"playlist exceeds the {MAX_PLAYLIST_TRACKS}-track safety limit; split it first"
            )
        return source, target, source_provider, target_provider

    def _apply_track_mappings(
        self, pair_id: int, account_id: int, state: PlaylistState
    ) -> PlaylistState:
        track_ids = {track.source_provider_track_id for track in state.tracks}
        if not track_ids:
            return state
        mappings = {
            mapping.provider_track_id: mapping.canonical_key
            for mapping in self.session.scalars(
                select(ProviderTrackMapping).where(
                    ProviderTrackMapping.pair_id == pair_id,
                    ProviderTrackMapping.account_id == account_id,
                    ProviderTrackMapping.identity_version == TRACK_IDENTITY_VERSION,
                    ProviderTrackMapping.provider_track_id.in_(track_ids),
                )
            )
        }
        if not mappings:
            return state
        return replace(
            state,
            tracks=tuple(
                replace(track, key=mappings.get(track.source_provider_track_id, track.key))
                for track in state.tracks
            ),
        )

    def _save_track_mapping(
        self,
        pair_id: int,
        account_id: int,
        track: ProviderTrack,
        canonical_key: str,
        *,
        source_track: ProviderTrack | None = None,
        provenance: str = "successful_add",
    ) -> None:
        mapping = self.session.scalar(
            select(ProviderTrackMapping).where(
                ProviderTrackMapping.pair_id == pair_id,
                ProviderTrackMapping.account_id == account_id,
                ProviderTrackMapping.provider_track_id == track.provider_track_id,
            )
        )
        if mapping is None:
            mapping = ProviderTrackMapping(
                pair_id=pair_id,
                account_id=account_id,
                provider_track_id=track.provider_track_id,
                canonical_key=canonical_key,
                source_provider_track_id=(
                    source_track.provider_track_id if source_track is not None else None
                ),
                source_isrc=_normalized_isrc(source_track.isrc) if source_track else None,
                provenance=provenance,
                identity_version=TRACK_IDENTITY_VERSION,
            )
        elif mapping.canonical_key != canonical_key:
            raise TrackMappingConflict(
                "a verified track identity conflicts with this pair; review it manually"
            )
        else:
            mapping.identity_version = TRACK_IDENTITY_VERSION
            if source_track is not None:
                mapping.source_provider_track_id = source_track.provider_track_id
                mapping.source_isrc = _normalized_isrc(source_track.isrc)
        self.session.add(mapping)

    def _save_resolved_track_mappings(
        self, pair: SyncPair, action: ReconciliationAction, resolved_track: ProviderTrack
    ) -> None:
        """Save both directions after a provider accepts an approved addition."""

        source_track = _action_provider_track(action)
        destination_account_id = (
            pair.source_account_id if action.side is Side.SOURCE else pair.target_account_id
        )
        origin_account_id = (
            pair.target_account_id if action.side is Side.SOURCE else pair.source_account_id
        )
        self._save_track_mapping(
            pair.id,
            destination_account_id,
            resolved_track,
            action.track.key,
            source_track=source_track,
        )
        # A reciprocal record means a successful YouTube -> Spotify match is
        # immediately reusable when the next approved change goes the other way.
        self._save_track_mapping(
            pair.id,
            origin_account_id,
            source_track,
            action.track.key,
            source_track=resolved_track,
            provenance="reciprocal_add",
        )

    @staticmethod
    def _provider_identity_score(left: ProviderTrack, right: ProviderTrack) -> int:
        """Return strong provider evidence that two references are one recording."""

        if left.provider_track_id == right.provider_track_id:
            return 3
        left_isrc = _normalized_isrc(left.isrc)
        right_isrc = _normalized_isrc(right.isrc)
        return 2 if left_isrc is not None and left_isrc == right_isrc else 0

    @classmethod
    def _existing_destination_equivalences(
        cls,
        plan: ReconciliationPlan,
        resolutions: dict[int, ProviderTrack],
        source: PlaylistState,
        target: PlaylistState,
    ) -> tuple[EquivalentTrackPair, ...]:
        """Link additions whose resolved recording already exists at destination.

        Reconciliation starts from portable text keys, so provider display
        metadata can initially make one recording look absent from both sides.
        If a strict destination search resolves to an item already present on
        that destination playlist, the apparent add is an identity mismatch,
        not a missing occurrence. Persist the equivalence and rebuild the plan;
        occurrence-aware reconciliation will still retain any real count gap.
        """

        destination_tracks = {
            Side.SOURCE: source.tracks,
            Side.TARGET: target.tracks,
        }
        edges: list[tuple[int, int, TrackState]] = []
        for index, action in enumerate(plan.actions):
            if action.action is not ActionType.ADD_TRACK:
                continue
            resolved = resolutions.get(index)
            if resolved is None:
                continue
            for existing in destination_tracks[action.side]:
                # Once both sides already share a canonical key, an existing
                # destination occurrence represents a real count difference,
                # not an identity mismatch.  It must remain an add.
                if action.track.key == existing.key:
                    continue
                score = cls._provider_identity_score(resolved, _state_provider_track(existing))
                if score:
                    edges.append((score, index, existing))

        matched_source_ids: set[str] = set()
        matched_target_ids: set[str] = set()
        equivalents: list[EquivalentTrackPair] = []
        for _score, action_index, existing in sorted(
            edges,
            key=lambda edge: (
                -edge[0],
                edge[1],
                edge[2].position if edge[2].position is not None else -1,
            ),
        ):
            action = plan.actions[action_index]
            origin_track = _action_provider_track(action)
            existing_track = _state_provider_track(existing)
            if action.side is Side.TARGET:
                source_track = origin_track
                target_track = existing_track
                canonical_key = action.track.key
            else:
                source_track = existing_track
                target_track = origin_track
                canonical_key = existing.key
            if (
                source_track.provider_track_id in matched_source_ids
                or target_track.provider_track_id in matched_target_ids
            ):
                continue
            matched_source_ids.add(source_track.provider_track_id)
            matched_target_ids.add(target_track.provider_track_id)
            equivalents.append(
                EquivalentTrackPair(
                    source_track=source_track,
                    target_track=target_track,
                    canonical_key=canonical_key,
                )
            )
        return tuple(equivalents)

    def _save_equivalent_track_mappings(
        self, pair: SyncPair, equivalents: Sequence[EquivalentTrackPair]
    ) -> None:
        """Persist verified no-op identities so reconciliation can be rebuilt."""

        for equivalent in equivalents:
            existing_keys = {
                mapping.canonical_key
                for account_id, provider_track_id in (
                    (pair.source_account_id, equivalent.source_track.provider_track_id),
                    (pair.target_account_id, equivalent.target_track.provider_track_id),
                )
                if (
                    mapping := self.session.scalar(
                        select(ProviderTrackMapping).where(
                            ProviderTrackMapping.pair_id == pair.id,
                            ProviderTrackMapping.account_id == account_id,
                            ProviderTrackMapping.provider_track_id == provider_track_id,
                        )
                    )
                )
                is not None
            }
            if len(existing_keys) > 1:
                raise TrackMappingConflict(
                    "existing verified identities disagree for equivalent playlist tracks"
                )
            canonical_key = next(iter(existing_keys), equivalent.canonical_key)
            self._save_track_mapping(
                pair.id,
                pair.source_account_id,
                equivalent.source_track,
                canonical_key,
                source_track=equivalent.target_track,
                provenance="verified_equivalence",
            )
            self._save_track_mapping(
                pair.id,
                pair.target_account_id,
                equivalent.target_track,
                canonical_key,
                source_track=equivalent.source_track,
                provenance="verified_equivalence",
            )

    def _assert_resolved_mapping_compatibility(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        resolutions: dict[int, ProviderTrack],
    ) -> None:
        """Reject a conflicting manual/cache choice before any playlist write.

        A provider video can legitimately be added twice, but it must never be
        silently declared to be two different canonical songs for the same
        pair.  This preflight closes the otherwise post-write conflict path.
        """

        pending: dict[tuple[int, str], str] = {}
        for index, resolved_track in resolutions.items():
            if not 0 <= index < len(plan.actions):
                raise TrackMappingConflict(
                    "the persisted review contains an invalid track resolution"
                )
            action = plan.actions[index]
            if action.action is not ActionType.ADD_TRACK:
                continue
            destination_account_id = (
                pair.source_account_id if action.side is Side.SOURCE else pair.target_account_id
            )
            origin_account_id = (
                pair.target_account_id if action.side is Side.SOURCE else pair.source_account_id
            )
            source_track = _action_provider_track(action)
            for account_id, provider_track_id in (
                (destination_account_id, resolved_track.provider_track_id),
                (origin_account_id, source_track.provider_track_id),
            ):
                pending_key = pending.get((account_id, provider_track_id))
                if pending_key is not None and pending_key != action.track.key:
                    raise TrackMappingConflict(
                        "this review maps one provider recording to different songs; "
                        "create a fresh review"
                    )
                pending[(account_id, provider_track_id)] = action.track.key
                existing = self.session.scalar(
                    select(ProviderTrackMapping).where(
                        ProviderTrackMapping.pair_id == pair.id,
                        ProviderTrackMapping.account_id == account_id,
                        ProviderTrackMapping.provider_track_id == provider_track_id,
                    )
                )
                if existing is not None and existing.canonical_key != action.track.key:
                    raise TrackMappingConflict(
                        "a selected recording is already verified as a different song; "
                        "choose another candidate"
                    )

    def _assert_no_partial_first_sync_artifacts(
        self,
        pair: SyncPair,
        source: PlaylistState,
        target: PlaylistState,
    ) -> None:
        """Do not propagate duplicate occurrences created by a failed first sync.

        A completed add in a failed run is harmless when the corresponding
        recording counts are now equal across providers: that write either
        completed a legitimate add or has already been reconciled. Unequal
        counts mean a later review would mirror a partial write as though it
        were an intentional user edit. Fail closed until the extra occurrence
        is removed or an operator explicitly establishes a new baseline.
        """

        if SyncBaselineRepository(self.session).latest_for_pair(pair.id) is not None:
            return
        failed_runs = list(
            self.session.scalars(
                select(SyncRun)
                .where(
                    SyncRun.pair_id == pair.id,
                    SyncRun.status == "failed",
                    SyncRun.plan_json.is_not(None),
                    SyncRun.resolution_json.is_not(None),
                )
                .order_by(SyncRun.id.desc())
                .limit(50)
            )
        )
        source_counts = Counter(track.source_provider_track_id for track in source.tracks)
        target_counts = Counter(track.source_provider_track_id for track in target.tracks)
        mismatches: set[str] = set()
        for failed_run in failed_runs:
            try:
                failed_plan = decode_plan(failed_run.plan_json or "")
                failed_resolutions = _decode_resolutions(failed_run.resolution_json)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            completed_ordinals = set(
                self.session.scalars(
                    select(SyncAction.ordinal).where(
                        SyncAction.run_id == failed_run.id,
                        SyncAction.status == "completed",
                        SyncAction.operation == ActionType.ADD_TRACK.value,
                    )
                )
            )
            for ordinal in completed_ordinals:
                if not 0 <= ordinal < len(failed_plan.actions):
                    continue
                action = failed_plan.actions[ordinal]
                resolved = failed_resolutions.get(ordinal)
                if action.action is not ActionType.ADD_TRACK or resolved is None:
                    continue
                if action.side is Side.SOURCE:
                    origin_count = target_counts[action.track.source_provider_track_id]
                    destination_count = source_counts[resolved.provider_track_id]
                else:
                    origin_count = source_counts[action.track.source_provider_track_id]
                    destination_count = target_counts[resolved.provider_track_id]
                if destination_count > origin_count:
                    mismatches.add(resolved.title)
        if mismatches:
            raise PartialSyncRecoveryRequired(
                "A previous first sync stopped with extra copies on the receiving playlist: "
                + "; ".join(sorted(mismatches))
                + ". Check these copies in the provider playlists before reviewing again. "
                "OPS has paused this review to avoid copying them back."
            )

    def _save_baseline(
        self, pair: SyncPair, source: PlaylistState, target: PlaylistState
    ) -> SyncBaseline:
        baseline = SyncBaseline(
            pair_id=pair.id,
            account_id=pair.source_account_id,
            playlist_key=f"{pair.source_playlist_id}:{pair.target_playlist_id}",
            source_provider=source.provider,
            target_provider=target.provider,
            snapshot_json=encode_baseline(BaselineState(source=source, target=target)),
            identity_version=TRACK_IDENTITY_VERSION,
            synchronized_at=datetime.now(UTC),
        )
        return SyncBaselineRepository(self.session).save(baseline)

    @staticmethod
    def _with_acknowledged_additions(
        state: PlaylistState,
        before: PlaylistState,
        additions: Sequence[tuple[ReconciliationAction, ProviderTrack]],
        side: Side,
    ) -> PlaylistState:
        """Keep an acknowledged write in the next baseline during provider read lag.

        Some provider playlist listings lag behind a successful mutation.  The
        provider accepted the write and OPS already saved a verified provider-ID
        mapping, but a first re-read can still omit the new occurrence.  Without
        this small overlay, its later appearance looks like a new reverse-side
        addition and can create a duplicate.  The overlay only fills a missing
        occurrence up to the exact pre-write count plus acknowledged adds; it
        never collapses real duplicate occurrences or guesses a match.
        """

        expected = Counter(track.key for track in before.tracks)
        expected.update(action.track.key for action, _ in additions if action.side is side)
        visible = Counter(track.key for track in state.tracks)
        missing = {
            key: max(0, expected_count - visible[key]) for key, expected_count in expected.items()
        }
        delayed: list[TrackState] = []
        for action, resolved in additions:
            if action.side is not side or not missing.get(action.track.key):
                continue
            delayed.append(replace(TrackState.from_provider_track(resolved), key=action.track.key))
            missing[action.track.key] -= 1
        return replace(state, tracks=state.tracks + tuple(delayed)) if delayed else state

    def _build_plan(
        self, pair: SyncPair, source: PlaylistState, target: PlaylistState
    ) -> tuple[ReconciliationPlan, SyncBaseline | None, bool]:
        baseline_record = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        if baseline_record and baseline_record.identity_version != TRACK_IDENTITY_VERSION:
            return (
                ReconciliationPlan(
                    actions=(),
                    conflicts=(),
                    initial_sync=True,
                    initial_policy=InitialSyncPolicy.ACCEPT_AS_IS,
                ),
                baseline_record,
                True,
            )
        baseline = decode_baseline(baseline_record.snapshot_json) if baseline_record else None
        if baseline is not None:
            # Interpret the immutable snapshot with the same verified aliases
            # as today's playlists. Otherwise linking an existing recording
            # would look like a removal of its old text key plus a new add.
            baseline = BaselineState(
                source=self._apply_track_mappings(pair.id, pair.source_account_id, baseline.source),
                target=self._apply_track_mappings(pair.id, pair.target_account_id, baseline.target),
            )
        return (
            reconcile(
                baseline,
                source,
                target,
                initial_policy=self._policy(pair),
                mode=self._mode(pair),
            ),
            baseline_record,
            False,
        )

    def _cached_resolutions(
        self, pair: SyncPair, plan: ReconciliationPlan
    ) -> dict[int, ProviderTrack]:
        additions = [
            (index, action)
            for index, action in enumerate(plan.actions)
            if action.action is ActionType.ADD_TRACK
        ]
        if not additions:
            return {}
        account_for_side = {
            Side.SOURCE: pair.source_account_id,
            Side.TARGET: pair.target_account_id,
        }
        account_ids = set(account_for_side.values())
        keys = {action.track.key for _, action in additions}
        source_track_ids = {action.track.source_provider_track_id for _, action in additions}
        source_isrcs = {
            isrc
            for _, action in additions
            if (isrc := _normalized_isrc(action.track.isrc)) is not None
        }
        conditions = [ProviderTrackMapping.canonical_key.in_(keys)]
        if source_track_ids:
            conditions.append(ProviderTrackMapping.source_provider_track_id.in_(source_track_ids))
        if source_isrcs:
            conditions.append(ProviderTrackMapping.source_isrc.in_(source_isrcs))
        mappings = list(
            self.session.scalars(
                select(ProviderTrackMapping)
                .where(
                    ProviderTrackMapping.pair_id == pair.id,
                    ProviderTrackMapping.account_id.in_(account_ids),
                    ProviderTrackMapping.identity_version == TRACK_IDENTITY_VERSION,
                    or_(*conditions),
                )
                .order_by(ProviderTrackMapping.updated_at.desc(), ProviderTrackMapping.id.desc())
            )
        )
        mapping_by_account_and_source_id: dict[tuple[int, str], ProviderTrackMapping] = {}
        mapping_by_account_and_isrc: dict[tuple[int, str], ProviderTrackMapping] = {}
        mapping_by_account_and_key: dict[tuple[int, str], ProviderTrackMapping] = {}
        for mapping in mappings:
            if mapping.source_provider_track_id:
                mapping_by_account_and_source_id.setdefault(
                    (mapping.account_id, mapping.source_provider_track_id), mapping
                )
            if mapping.source_isrc:
                mapping_by_account_and_isrc.setdefault(
                    (mapping.account_id, mapping.source_isrc), mapping
                )
            mapping_by_account_and_key.setdefault(
                (mapping.account_id, mapping.canonical_key), mapping
            )
        resolutions: dict[int, ProviderTrack] = {}
        for index, action in additions:
            account_id = account_for_side[action.side]
            mapping = mapping_by_account_and_source_id.get(
                (account_id, action.track.source_provider_track_id)
            )
            if mapping is None and (isrc := _normalized_isrc(action.track.isrc)) is not None:
                mapping = mapping_by_account_and_isrc.get((account_id, isrc))
            if mapping is None:
                mapping = mapping_by_account_and_key.get((account_id, action.track.key))
            if mapping is not None:
                resolutions[index] = ProviderTrack(
                    provider_track_id=mapping.provider_track_id,
                    title=action.track.title,
                    artists=action.track.artists,
                    album=action.track.album,
                    duration_ms=action.track.duration_ms,
                    isrc=action.track.isrc,
                )
        return resolutions

    def _cached_search_result(
        self, account_id: int, provider_name: str, track: ProviderTrack
    ) -> tuple[ProviderTrack | None, tuple[ProviderTrack, ...]] | None:
        cache = self.session.scalar(
            select(ProviderSearchCache).where(
                ProviderSearchCache.account_id == account_id,
                ProviderSearchCache.provider_name == provider_name,
                ProviderSearchCache.track_fingerprint == self._search_cache_fingerprint(track),
                ProviderSearchCache.algorithm_version == SEARCH_CACHE_ALGORITHM_VERSION,
            )
        )
        if cache is None or _utc(cache.expires_at) <= datetime.now(UTC):
            return None
        try:
            resolved_payload = (
                json.loads(cache.resolved_track_json) if cache.resolved_track_json else None
            )
            resolved = (
                _provider_track_from_dict(resolved_payload)
                if isinstance(resolved_payload, dict)
                else None
            )
            candidates_payload = json.loads(cache.candidate_tracks_json or "[]")
            candidates = tuple(
                _provider_track_from_dict(candidate)
                for candidate in candidates_payload
                if isinstance(candidate, dict)
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        return resolved, candidates

    def _save_search_result(
        self,
        account_id: int,
        provider_name: str,
        track: ProviderTrack,
        resolved: ProviderTrack | None,
        candidates: Sequence[ProviderTrack],
    ) -> None:
        fingerprint = self._search_cache_fingerprint(track)
        cache = self.session.scalar(
            select(ProviderSearchCache).where(
                ProviderSearchCache.account_id == account_id,
                ProviderSearchCache.provider_name == provider_name,
                ProviderSearchCache.track_fingerprint == fingerprint,
                ProviderSearchCache.algorithm_version == SEARCH_CACHE_ALGORITHM_VERSION,
            )
        )
        if cache is None:
            cache = ProviderSearchCache(
                account_id=account_id,
                provider_name=provider_name,
                track_fingerprint=fingerprint,
                algorithm_version=SEARCH_CACHE_ALGORITHM_VERSION,
            )
        cache.resolved_track_json = (
            json.dumps(_provider_track_dict(resolved), sort_keys=True, separators=(",", ":"))
            if resolved is not None
            else None
        )
        cache.candidate_tracks_json = (
            json.dumps(
                [_provider_track_dict(candidate) for candidate in candidates],
                sort_keys=True,
                separators=(",", ":"),
            )
            if candidates
            else None
        )
        cache.expires_at = datetime.now(UTC) + (
            RESOLVED_SEARCH_CACHE_TTL if resolved is not None else UNRESOLVED_SEARCH_CACHE_TTL
        )
        self.session.add(cache)

    def _invalidate_stale_resolution(
        self, pair: SyncPair, action: ReconciliationAction, resolved_track: ProviderTrack
    ) -> None:
        """Do not repeat a mapping/search result a provider rejected after review."""

        destination_account_id = (
            pair.source_account_id if action.side is Side.SOURCE else pair.target_account_id
        )
        origin_account_id = (
            pair.target_account_id if action.side is Side.SOURCE else pair.source_account_id
        )
        source_track = _action_provider_track(action)
        destination_account = self.session.get(ProviderAccount, destination_account_id)
        if destination_account is None:
            return
        self.session.execute(
            delete(ProviderTrackMapping).where(
                ProviderTrackMapping.pair_id == pair.id,
                ProviderTrackMapping.account_id == destination_account_id,
                ProviderTrackMapping.provider_track_id == resolved_track.provider_track_id,
            )
        )
        self.session.execute(
            delete(ProviderTrackMapping).where(
                ProviderTrackMapping.pair_id == pair.id,
                ProviderTrackMapping.account_id == origin_account_id,
                ProviderTrackMapping.provider_track_id == source_track.provider_track_id,
                ProviderTrackMapping.source_provider_track_id == resolved_track.provider_track_id,
            )
        )
        self.session.execute(
            delete(ProviderSearchCache).where(
                ProviderSearchCache.account_id == destination_account_id,
                ProviderSearchCache.provider_name == destination_account.provider_name,
                ProviderSearchCache.track_fingerprint
                == self._search_cache_fingerprint(source_track),
                ProviderSearchCache.algorithm_version == SEARCH_CACHE_ALGORITHM_VERSION,
            )
        )

    def _resolve_additions(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        source_provider: SyncProvider,
        target_provider: SyncProvider,
        source: PlaylistState | None = None,
        target: PlaylistState | None = None,
    ) -> tuple[dict[int, ProviderTrack], tuple[int, ...], dict[int, tuple[ProviderTrack, ...]]]:
        resolutions = self._cached_resolutions(pair, plan)
        unresolved: list[int] = []
        candidates_by_index: dict[int, tuple[ProviderTrack, ...]] = {}
        by_destination_and_fingerprint: dict[
            tuple[Side, str], tuple[ProviderTrack | None, tuple[ProviderTrack, ...]]
        ] = {}
        account_for_side = {
            Side.SOURCE: pair.source_account_id,
            Side.TARGET: pair.target_account_id,
        }
        lookups = 0
        for index, action in enumerate(plan.actions):
            if action.action is not ActionType.ADD_TRACK or index in resolutions:
                continue
            provider = source_provider if action.side is Side.SOURCE else target_provider
            provider_track = _action_provider_track(action)
            destination = source if action.side is Side.SOURCE else target
            resolve_existing = getattr(provider, "resolve_search_candidates", None)
            if destination is not None and destination.tracks and callable(resolve_existing):
                # Prefer a verified recording already in this playlist over a
                # catalogue search that may favour a different release. Reuse
                # the provider's full version/artist/ambiguity rules.
                local_match = resolve_existing(
                    provider_track,
                    tuple(_state_provider_track(track) for track in destination.tracks),
                )
                if local_match is not None:
                    resolutions[index] = local_match
                    continue
            local_match = self._resolve_explicit_preference(
                provider,
                provider_track,
                tuple(_state_provider_track(track) for track in destination.tracks)
                if destination is not None
                else (),
            )
            if local_match is not None:
                resolutions[index] = local_match
                continue
            lookup_key = (action.side, self._search_cache_fingerprint(provider_track))
            if lookup_key in by_destination_and_fingerprint:
                resolved, candidates = by_destination_and_fingerprint[lookup_key]
            else:
                cached = self._cached_search_result(
                    account_for_side[action.side], provider.name, provider_track
                )
                if cached is not None:
                    resolved, candidates = cached
                    if resolved is None:
                        resolved = self._resolve_explicit_preference(
                            provider, provider_track, candidates
                        )
                else:
                    lookups += 1
                    if lookups > MAX_REVIEW_LOOKUPS:
                        raise ValueError(
                            f"review requires more than {MAX_REVIEW_LOOKUPS} provider searches; "
                            "split the playlist or establish a trusted baseline"
                        )
                    resolved = provider.search_track(provider_track)
                    candidate_lookup = getattr(provider, "close_track_candidates", None)
                    candidates = (
                        tuple(candidate_lookup(provider_track))[:MAX_MANUAL_CANDIDATES]
                        if resolved is None and callable(candidate_lookup)
                        else ()
                    )
                    if resolved is None:
                        resolved = self._resolve_explicit_preference(
                            provider, provider_track, candidates
                        )
                    if resolved is None and candidates and lookups < MAX_REVIEW_LOOKUPS:
                        # Some source providers omit release metadata from their
                        # playlist endpoint. Ask for it only after an ordinary
                        # destination search is ambiguous, then re-score the
                        # existing candidates without another destination API
                        # search. This keeps the common path quota-efficient.
                        origin_provider = (
                            target_provider if action.side is Side.SOURCE else source_provider
                        )
                        enrich = getattr(origin_provider, "enrich_track_metadata", None)
                        resolve_existing = getattr(provider, "resolve_search_candidates", None)
                        if callable(enrich) and callable(resolve_existing):
                            lookups += 1
                            try:
                                enriched_track = enrich(provider_track)
                                if enriched_track != provider_track:
                                    resolved = resolve_existing(enriched_track, candidates)
                                    if resolved is None:
                                        resolved = self._resolve_explicit_preference(
                                            provider, enriched_track, candidates
                                        )
                            except ProviderError:
                                # Optional enrichment must never turn a usable
                                # manual review into a failed review.
                                resolved = None
                    self._save_search_result(
                        account_for_side[action.side],
                        provider.name,
                        provider_track,
                        resolved,
                        candidates,
                    )
                by_destination_and_fingerprint[lookup_key] = (resolved, candidates)
            if resolved is None:
                unresolved.append(index)
                if candidates:
                    candidates_by_index[index] = candidates
            else:
                resolutions[index] = resolved
        return resolutions, tuple(unresolved), candidates_by_index

    def _prepared_from_run(
        self,
        run: SyncRun,
        token: str = "",  # nosec B107
    ) -> PreparedReview:
        if not run.plan_json:
            raise ReviewNotApplicable("the selected review has no persisted plan")
        plan = decode_plan(run.plan_json)
        summary = {}
        try:
            summary = json.loads(run.summary_json or "{}")
            if not isinstance(summary, dict):
                summary = {}
            unresolved_indices = tuple(
                int(value) for value in summary.get("unresolved_indices", ())
            )
            source_track_count = summary.get("source_track_count")
            target_track_count = summary.get("target_track_count")
        except (TypeError, ValueError, json.JSONDecodeError):
            unresolved_indices = ()
            source_track_count = None
            target_track_count = None
        source_track_count = source_track_count if isinstance(source_track_count, int) else None
        target_track_count = target_track_count if isinstance(target_track_count, int) else None
        unresolved_actions = tuple(
            plan.actions[index] for index in unresolved_indices if 0 <= index < len(plan.actions)
        )
        candidate_options = tuple(
            ManualCandidateOptions(index, plan.actions[index], candidates)
            for index, candidates in _decode_candidates(run.candidate_json).items()
            if index in unresolved_indices and 0 <= index < len(plan.actions)
        )
        return PreparedReview(
            review_id=run.id,
            plan=plan,
            unresolved_actions=unresolved_actions,
            approval_token=token,
            status=run.status,
            approval_expires_at=run.approval_expires_at,
            candidate_options=candidate_options,
            source_track_count=source_track_count,
            target_track_count=target_track_count,
            source_name=summary.get("source_name"),
            target_name=summary.get("target_name"),
            replacement=bool(summary.get("manual_replacement")),
            replacement_track=_decode_resolutions(run.resolution_json).get(0)
            if summary.get("manual_replacement")
            else None,
        )

    def load_review(self, pair: SyncPair, review_id: int | None = None) -> PreparedReview | None:
        run_repo = SyncRunRepository(self.session)
        run = (
            run_repo.get(review_id)
            if review_id is not None
            else run_repo.latest_open_review(pair.id)
        )
        if run is None:
            return None
        if run.pair_id != pair.id:
            raise ReviewNotApplicable("the selected review does not belong to this playlist pair")
        return self._prepared_from_run(run)

    def prepare_review(self, pair: SyncPair) -> PreparedReview:
        """Create or briefly reuse one bounded, persisted, state-bound review."""

        lease = acquire_pair_lease(self.session, pair.id)
        run: SyncRun | None = None
        try:
            now = datetime.now(UTC)
            run_repo = SyncRunRepository(self.session)
            existing = run_repo.latest_open_review(pair.id)
            if (
                existing is not None
                and not json.loads(existing.summary_json or "{}").get("manual_replacement")
                and existing.approval_expires_at is not None
                and _utc(existing.approval_expires_at) > now
                and existing.started_at is not None
                and _utc(existing.started_at) + REVIEW_REUSE_WINDOW > now
                and self._review_context_matches(pair, existing)
            ):
                token = secrets.token_urlsafe(32)
                existing.approval_token_hash = _token_hash(token)
                self.session.commit()
                return self._prepared_from_run(existing, token)

            latest = run_repo.latest_for_pair(pair.id)
            if (
                latest is not None
                and latest.status in {"preparing", "review_failed"}
                and not self._is_authorization_failure(latest)
                and latest.started_at is not None
                and _utc(latest.started_at) + REVIEW_REUSE_WINDOW > now
            ):
                raise PairOperationBusy(
                    "a review was just attempted for this pair; wait two minutes before retrying"
                )

            # Commit the attempt before contacting either provider. Even a failed
            # or over-limit review therefore creates a short-lived quota boundary.
            run = run_repo.start(pair_id=pair.id)
            run.status = "preparing"
            self.session.add(run)
            self.session.commit()

            source, target, source_provider, target_provider = self._current_state(pair)
            self._assert_no_partial_first_sync_artifacts(pair, source, target)
            plan, baseline_record, baseline_upgrade = self._build_plan(pair, source, target)
            if len(plan.actions) > MAX_PLAN_ACTIONS:
                raise ValueError(
                    f"review contains more than {MAX_PLAN_ACTIONS} actions; split the playlist"
                )
            resolutions: dict[int, ProviderTrack] = {}
            unresolved_indices: tuple[int, ...] = ()
            candidate_options: dict[int, tuple[ProviderTrack, ...]] = {}
            if not baseline_upgrade and not plan.conflicts:
                # A first merge can contain reciprocal additions when provider
                # display metadata gives an existing recording different text
                # keys. Resolve and persist those verified equivalences, then
                # rebuild the plan so neither playlist receives a duplicate.
                for _pass in range(4):
                    resolutions, unresolved_indices, candidate_options = self._resolve_additions(
                        pair, plan, source_provider, target_provider, source, target
                    )
                    equivalents = self._existing_destination_equivalences(
                        plan, resolutions, source, target
                    )
                    if not equivalents:
                        break
                    self._save_equivalent_track_mappings(pair, equivalents)
                    self.session.flush()
                    source = self._apply_track_mappings(pair.id, pair.source_account_id, source)
                    target = self._apply_track_mappings(pair.id, pair.target_account_id, target)
                    plan, baseline_record, baseline_upgrade = self._build_plan(pair, source, target)
                else:
                    raise TrackMappingConflict("playlist identities could not be stabilized safely")
                self._assert_resolved_mapping_compatibility(pair, plan, resolutions)
            token = secrets.token_urlsafe(32)
            run.baseline_id = baseline_record.id if baseline_record else None
            run.plan_fingerprint = plan_fingerprint(plan)
            run.status = (
                "baseline_upgrade"
                if baseline_upgrade
                else "conflict"
                if plan.conflicts
                else "planned"
            )
            run.plan_json = encode_plan(plan)
            run.resolution_json = _encode_resolutions(resolutions)
            run.candidate_json = _encode_candidates(candidate_options)
            run.source_state_hash = playlist_state_hash(source)
            run.target_state_hash = playlist_state_hash(target)
            run.approval_token_hash = _token_hash(token)
            run.approval_expires_at = datetime.now(UTC) + REVIEW_APPROVAL_TTL
            run.summary_json = json.dumps(
                {
                    "actions": len(plan.actions),
                    "conflicts": len(plan.conflicts),
                    "initial_sync": plan.initial_sync,
                    "policy": plan.initial_policy.value if plan.initial_policy else None,
                    "source_track_count": len(source.tracks),
                    "target_track_count": len(target.tracks),
                    "source_name": source.name,
                    "target_name": target.name,
                    "pair_binding": self._pair_binding(pair),
                    "unresolved_indices": unresolved_indices,
                    "baseline_upgrade": baseline_upgrade,
                },
                sort_keys=True,
            )
            run.completed_at = datetime.now(UTC)
            self.session.add(run)
            self.session.flush()
            run_repo.prune_previews(pair.id)
            self.session.commit()
            return self._prepared_from_run(run, token)
        except Exception as exc:
            self.session.rollback()
            if run is not None:
                run_id = run.id
                failed_run = self.session.get(SyncRun, run_id)
                if failed_run is not None and failed_run.status == "preparing":
                    SyncRunRepository(self.session).finish(
                        failed_run,
                        "authorization_required"
                        if isinstance(exc, AuthorizationRequired)
                        else "review_failed",
                        json.dumps(
                            {
                                "error": "review could not be prepared",
                                "error_type": type(exc).__name__,
                            },
                            sort_keys=True,
                        ),
                    )
                    SyncRunRepository(self.session).prune_previews(pair.id)
                    self.session.commit()
            raise
        finally:
            lease.release()

    @staticmethod
    def _is_authorization_failure(run: SyncRun) -> bool:
        """Allow reconnects to be tested immediately, including old stored runs."""

        if run.status == "authorization_required":
            return True
        if run.status != "review_failed":
            return False
        try:
            summary = json.loads(run.summary_json or "{}")
        except (TypeError, ValueError):
            return False
        return summary.get("error_type") == "AuthorizationRequired"

    def prepare_replacement(self, pair: SyncPair, side: Side, track_id: str) -> PreparedReview:
        """Review a single recording replacement, never a background-sync decision."""
        lease = acquire_pair_lease(self.session, pair.id)
        try:
            if self._mode(pair) is SyncMode.SOURCE_TO_TARGET and side is Side.SOURCE:
                raise ReviewNotApplicable("The source is read-only in Follow source mode")
            source, target, source_provider, target_provider = self._current_state(pair)
            normal, baseline, upgrade = self._build_plan(pair, source, target)
            if baseline is None or upgrade or normal.actions or normal.conflicts:
                raise ReviewNotApplicable(
                    "Finish the current sync review before changing a saved match"
                )
            destination = source if side is Side.SOURCE else target
            origin = target if side is Side.SOURCE else source
            provider = source_provider if side is Side.SOURCE else target_provider
            old = [t for t in destination.tracks if t.source_provider_track_id == track_id]
            if len(old) != 1:
                raise ReviewNotApplicable(
                    "Choose a single track occurrence; duplicate copies need manual repair first"
                )
            counterpart = [t for t in origin.tracks if t.key == old[0].key]
            if len(counterpart) != 1:
                raise ReviewNotApplicable("This track does not have a unique saved counterpart")
            search = getattr(provider, "close_track_candidates", None)
            if not callable(search):
                raise ReviewNotApplicable("This provider cannot offer alternative recordings")
            present = {t.source_provider_track_id for t in destination.tracks}
            candidates = tuple(
                {
                    c.provider_track_id: c
                    for c in search(_state_provider_track(counterpart[0]))
                    if c.provider_track_id not in present
                }.values()
            )[:MAX_MANUAL_CANDIDATES]
            if not candidates:
                raise ReviewNotApplicable(
                    "No alternative recordings were found; the current version is unchanged"
                )
            plan = ReconciliationPlan(
                actions=(
                    ReconciliationAction(
                        side,
                        ActionType.ADD_TRACK,
                        counterpart[0],
                        "Add manually selected replacement",
                    ),
                    ReconciliationAction(
                        side,
                        ActionType.REMOVE_TRACK,
                        old[0],
                        "Remove previous matched recording after replacement succeeds",
                    ),
                ),
                conflicts=(),
            )
            self._invalidate_reviews(pair.id)
            run = SyncRunRepository(self.session).start(baseline.id, pair_id=pair.id)
            token = secrets.token_urlsafe(32)
            run.status = "planned"
            run.plan_json = encode_plan(plan)
            run.plan_fingerprint = plan_fingerprint(plan)
            run.candidate_json = _encode_candidates({0: candidates})
            run.resolution_json = _encode_resolutions({})
            run.source_state_hash = playlist_state_hash(source)
            run.target_state_hash = playlist_state_hash(target)
            run.approval_token_hash = _token_hash(token)
            run.approval_expires_at = datetime.now(UTC) + REVIEW_APPROVAL_TTL
            run.completed_at = datetime.now(UTC)
            run.summary_json = json.dumps(
                {
                    "manual_replacement": True,
                    "unresolved_indices": [0],
                    "pair_binding": self._pair_binding(pair),
                    "source_name": source.name,
                    "target_name": target.name,
                    "source_track_count": len(source.tracks),
                    "target_track_count": len(target.tracks),
                }
            )
            self.session.commit()
            return self._prepared_from_run(run, token)
        finally:
            lease.release()

    def select_candidate(
        self,
        pair: SyncPair,
        review_id: int,
        action_index: int,
        candidate_id: str,
    ) -> PreparedReview:
        """Persist one operator-approved close match without changing a playlist."""

        lease = acquire_pair_lease(self.session, pair.id)
        try:
            run = self.session.get(SyncRun, review_id)
            if run is None or run.pair_id != pair.id or run.status != "planned":
                raise ReviewNotApplicable("the selected review is no longer available")
            if run.approval_consumed_at is not None:
                raise ReviewNotApplicable("the selected review was already applied")
            if run.approval_expires_at is None or _utc(run.approval_expires_at) <= datetime.now(
                UTC
            ):
                raise ReviewExpired("the selected review has expired; create a fresh review")
            if not run.plan_json:
                raise ReviewNotApplicable("the selected review has no persisted plan")

            plan = decode_plan(run.plan_json)
            summary = json.loads(run.summary_json or "{}")
            unresolved_indices = {int(value) for value in summary.get("unresolved_indices", ())}
            if action_index not in unresolved_indices or not 0 <= action_index < len(plan.actions):
                raise ReviewNotApplicable("that track is not awaiting a candidate choice")
            candidates = _decode_candidates(run.candidate_json)
            selected = next(
                (
                    candidate
                    for candidate in candidates.get(action_index, ())
                    if candidate.provider_track_id == candidate_id
                ),
                None,
            )
            if selected is None:
                raise ReviewNotApplicable("that candidate is not part of this review")

            resolutions = _decode_resolutions(run.resolution_json)
            resolutions[action_index] = selected
            unresolved_indices.remove(action_index)
            candidates.pop(action_index, None)
            summary["unresolved_indices"] = sorted(unresolved_indices)
            run.resolution_json = _encode_resolutions(resolutions)
            run.candidate_json = _encode_candidates(candidates)
            run.summary_json = json.dumps(summary, sort_keys=True)
            self.session.add(run)
            self.session.commit()
            return self._prepared_from_run(run)
        finally:
            lease.release()

    def preview(self, pair: SyncPair) -> ReconciliationPlan:
        """Compatibility boundary used by the scheduler and service tests."""

        return self.prepare_review(pair).plan

    def unresolved_actions(
        self, pair: SyncPair, plan: ReconciliationPlan
    ) -> tuple[ReconciliationAction, ...]:
        """Return unresolved additions from the persisted review, without provider calls."""

        run = SyncRunRepository(self.session).latest_open_review(pair.id)
        if run is None or not run.plan_json or decode_plan(run.plan_json) != plan:
            raise ReviewNotApplicable("create a fresh review before checking unresolved tracks")
        return self._prepared_from_run(run).unresolved_actions

    def accept_current_state(self, pair: SyncPair) -> None:
        lease = acquire_pair_lease(self.session, pair.id)
        try:
            source, target, _, _ = self._current_state(pair)
            baseline = self._save_baseline(pair, source, target)
            self._invalidate_reviews(pair.id)
            run_repo = SyncRunRepository(self.session)
            run = run_repo.start(baseline.id, pair_id=pair.id)
            run_repo.finish(run, "baseline_accepted")
            self.session.commit()
        finally:
            lease.release()

    @staticmethod
    def _ensure_unambiguous_spotify_removals(
        plan: ReconciliationPlan,
        source: PlaylistState,
        target: PlaylistState,
        source_provider: SyncProvider,
        target_provider: SyncProvider,
    ) -> None:
        for side, state, provider in (
            (Side.SOURCE, source, source_provider),
            (Side.TARGET, target, target_provider),
        ):
            if provider.name != "spotify":
                continue
            counts = Counter(track.source_provider_track_id for track in state.tracks)
            if any(
                action.action is ActionType.REMOVE_TRACK
                and action.side is side
                and counts[action.track.source_provider_track_id] > 1
                for action in plan.actions
            ):
                raise AmbiguousSpotifyRemoval(
                    "Spotify contains duplicate copies of a track selected for removal; "
                    "remove the intended duplicate manually, then create a new review"
                )

    def _consume_review(self, run: SyncRun, approval: Approval) -> None:
        now = datetime.now(UTC)
        if run.status != "planned" or run.approval_consumed_at is not None:
            raise ReviewNotApplicable("this review was already used or cannot be applied")
        if run.approval_expires_at is None or _utc(run.approval_expires_at) <= now:
            raise ReviewExpired("this review expired; create a new review")
        submitted_hash = _token_hash(approval.token)
        if not run.approval_token_hash or not secrets.compare_digest(
            submitted_hash, run.approval_token_hash
        ):
            raise DestructiveActionApprovalError("the one-time review approval is not valid")
        result = self.session.execute(
            update(SyncRun)
            .where(
                SyncRun.id == run.id,
                SyncRun.status == "planned",
                SyncRun.approval_consumed_at.is_(None),
                SyncRun.approval_token_hash == submitted_hash,
            )
            .values(status="applying", approval_consumed_at=now)
        )
        if result.rowcount != 1:
            self.session.rollback()
            raise ReviewNotApplicable("this review was already used or cannot be applied")
        self.session.commit()
        run.status = "applying"
        run.approval_consumed_at = now

    def apply(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        approval: Approval | None = None,
        *,
        skip_unresolved: bool = False,
    ) -> None:
        """Apply one persisted review exactly once while holding the pair lease."""

        if approval is None or approval.review_id is None or not approval.token:
            raise DestructiveActionApprovalError("a one-time review approval is required")
        lease = acquire_pair_lease(self.session, pair.id)
        run: SyncRun | None = None
        journal = []
        try:
            run = self.session.get(SyncRun, approval.review_id)
            if run is None or run.pair_id != pair.id or not run.plan_json:
                raise ReviewNotApplicable("the selected review does not belong to this pair")
            if run.status != "planned" or run.approval_consumed_at is not None:
                raise ReviewNotApplicable("this review was already used or cannot be applied")
            persisted_plan = decode_plan(run.plan_json)
            if persisted_plan != plan:
                raise ReviewNotApplicable("the submitted plan is not the persisted review")
            if approval.plan_fingerprint != (run.plan_fingerprint or ""):
                raise DestructiveActionApprovalError("the approval does not match the review")
            if not self._review_context_matches(pair, run):
                self._invalidate_reviews(pair.id)
                self.session.commit()
                raise ReviewNotApplicable(
                    "The sync direction or saved baseline changed since this review. "
                    "Review the latest changes before applying."
                )
            from ops.sync.safety import validate_approval

            validate_approval(plan, approval)
            current_source, current_target, source_provider, target_provider = self._current_state(
                pair
            )
            self._assert_no_partial_first_sync_artifacts(pair, current_source, current_target)
            if (
                playlist_state_hash(current_source) != run.source_state_hash
                or playlist_state_hash(current_target) != run.target_state_hash
            ):
                # Invalidate older previews too: otherwise the reuse shortcut can
                # hand the browser another obsolete review immediately afterwards.
                self.session.execute(
                    update(SyncRun)
                    .where(
                        SyncRun.pair_id == pair.id,
                        SyncRun.id <= run.id,
                        SyncRun.status.in_(("planned", "conflict", "baseline_upgrade")),
                    )
                    .values(status="stale", approval_token_hash=None)
                )
                self.session.commit()
                raise ValueError(
                    "The provider state changed since this review was prepared. "
                    "No changes were applied by this attempt. "
                    "Review the latest changes to continue."
                )
            self._ensure_unambiguous_spotify_removals(
                plan,
                current_source,
                current_target,
                source_provider,
                target_provider,
            )
            resolutions = _decode_resolutions(run.resolution_json)
            replacement = bool(json.loads(run.summary_json or "{}").get("manual_replacement"))
            if replacement and (skip_unresolved or 0 not in resolutions):
                raise ReviewNotApplicable(
                    "Choose a replacement before applying; the old version will not be removed"
                )
            if self._existing_destination_equivalences(
                plan, resolutions, current_source, current_target
            ):
                raise ReviewNotApplicable(
                    "This review would add a recording already present under different metadata. "
                    "Create a fresh review so OPS can link the existing copies."
                )
            self._assert_resolved_mapping_compatibility(pair, plan, resolutions)
            self._consume_review(run, approval)

            action_repo = SyncActionRepository(self.session)
            journal = [
                action_repo.plan(
                    run,
                    ordinal,
                    "source" if action.side is Side.SOURCE else "target",
                    action.action.value,
                    action.track.key,
                )
                for ordinal, action in enumerate(plan.actions)
            ]
            self.session.commit()

            def completed(index: int) -> None:
                action_repo.complete(journal[index])
                self.session.commit()
                lease.renew()

            acknowledged_additions: list[tuple[ReconciliationAction, ProviderTrack]] = []

            def save_resolved_addition(action: ReconciliationAction, track: ProviderTrack) -> None:
                self._save_resolved_track_mappings(pair, action, track)
                acknowledged_additions.append((action, track))

            result = SyncExecutor().apply(
                plan,
                source_provider=source_provider,
                target_provider=target_provider,
                source_playlist_id=pair.source_playlist_id,
                target_playlist_id=pair.target_playlist_id,
                source_snapshot_id=current_source.snapshot_id,
                target_snapshot_id=current_target.snapshot_id,
                approval=approval,
                skip_unresolved=skip_unresolved,
                fail_on_unavailable=replacement,
                pre_resolved_tracks=resolutions,
                on_action_completed=completed,
                on_track_resolved=save_resolved_addition,
                on_track_unavailable=lambda action, track: self._invalidate_stale_resolution(
                    pair, action, track
                ),
            )
            for index in result.skipped_indices:
                journal[index].status = "skipped"
                journal[index].error_summary = (
                    "destination provider rejected the selected track after review"
                    if index in result.provider_rejected_indices
                    else "track could not be resolved on the destination provider"
                )
            if result.skipped_indices:
                SyncRunRepository(self.session).finish(
                    run,
                    "partially_applied",
                    json.dumps(
                        {
                            "actions_applied": len(plan.actions) - len(result.skipped_indices),
                            "actions_skipped": len(result.skipped_indices),
                            "baseline_advanced": False,
                        }
                    ),
                )
            else:
                # Flush the mappings before re-reading.  If a provider is
                # briefly eventually consistent, retain only the accepted
                # addition(s) that the listing has not shown yet.
                self.session.flush()
                resulting_source, resulting_target, _, _ = self._current_state(pair)
                if replacement:
                    destination = (
                        resulting_source
                        if plan.actions[0].side is Side.SOURCE
                        else resulting_target
                    )
                    ids = [t.source_provider_track_id for t in destination.tracks]
                    if (
                        resolutions[0].provider_track_id not in ids
                        or plan.actions[1].track.source_provider_track_id in ids
                    ):
                        raise PlanExecutionError(
                            "The replacement was sent but is not yet visible. "
                            "Review current playlists before continuing."
                        )
                    # A replacement has a net count of zero. Do not synthesize
                    # an additional occurrence from the pre-removal snapshot.
                    current_source = resulting_source
                    current_target = resulting_target
                    acknowledged_additions = []
                resulting_source = self._with_acknowledged_additions(
                    resulting_source, current_source, acknowledged_additions, Side.SOURCE
                )
                resulting_target = self._with_acknowledged_additions(
                    resulting_target, current_target, acknowledged_additions, Side.TARGET
                )
                self._save_baseline(pair, resulting_source, resulting_target)
                SyncRunRepository(self.session).finish(
                    run,
                    "applied",
                    json.dumps(
                        {
                            "actions_applied": len(plan.actions),
                            "actions_skipped": 0,
                            "baseline_advanced": True,
                        }
                    ),
                )
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            if run is not None and run.status == "applying":
                run = self.session.get(SyncRun, run.id)
                if run is not None:
                    actions = SyncActionRepository(self.session).for_run(run.id)
                    for action in actions:
                        if action.status == "planned":
                            SyncActionRepository(self.session).fail(action, str(exc))
                            break
                    SyncRunRepository(self.session).finish(
                        run, "failed", json.dumps({"error": str(exc)[:500]})
                    )
                    self.session.commit()
            raise
        finally:
            lease.release()
