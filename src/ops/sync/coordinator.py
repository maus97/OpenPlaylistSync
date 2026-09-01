"""Persistence-aware review, one-time approval, and recoverable execution."""

import hashlib
import json
import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService
from ops.config import Settings
from ops.models import ProviderAccount, ProviderTrackMapping, SyncBaseline, SyncPair, SyncRun
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


class ReviewNotApplicable(ValueError):
    """Raised when a review does not belong to this pair or cannot be applied."""


class ReviewExpired(ValueError):
    """Raised when an operator approval outlives its short validity window."""


class TrackMappingConflict(PlanExecutionError):
    """Raised instead of silently overwriting a verified pair-scoped identity."""


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

    @property
    def baseline_upgrade_required(self) -> bool:
        return self.status == "baseline_upgrade"


@dataclass(frozen=True, slots=True)
class ManualCandidateOptions:
    """Bounded, persisted fallback choices that require an operator decision."""

    action_index: int
    action: ReconciliationAction
    candidates: tuple[ProviderTrack, ...]


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
        expires_at = credentials.get("expires_at")
        try:
            expired = not expires_at or datetime.fromisoformat(str(expires_at)).astimezone(
                UTC
            ) <= datetime.now(UTC)
        except ValueError:
            expired = True
        if not expired:
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
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            ).isoformat(),
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
        try:
            return InitialSyncPolicy(pair.initial_sync_policy)
        except ValueError:
            return InitialSyncPolicy.MERGE

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
                provenance="successful_add",
                identity_version=TRACK_IDENTITY_VERSION,
            )
        elif mapping.canonical_key != canonical_key:
            raise TrackMappingConflict(
                "a verified track identity conflicts with this pair; review it manually"
            )
        self.session.add(mapping)

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
        return (
            reconcile(baseline, source, target, initial_policy=self._policy(pair)),
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
        mappings = list(
            self.session.scalars(
                select(ProviderTrackMapping)
                .where(
                    ProviderTrackMapping.pair_id == pair.id,
                    ProviderTrackMapping.account_id.in_(account_ids),
                    ProviderTrackMapping.identity_version == TRACK_IDENTITY_VERSION,
                    ProviderTrackMapping.canonical_key.in_(keys),
                )
                .order_by(ProviderTrackMapping.updated_at.desc(), ProviderTrackMapping.id.desc())
            )
        )
        mapping_by_account_and_key: dict[tuple[int, str], ProviderTrackMapping] = {}
        for mapping in mappings:
            mapping_by_account_and_key.setdefault(
                (mapping.account_id, mapping.canonical_key), mapping
            )
        return {
            index: ProviderTrack(
                provider_track_id=mapping.provider_track_id,
                title=action.track.title,
                artists=action.track.artists,
                duration_ms=action.track.duration_ms,
                isrc=action.track.isrc,
            )
            for index, action in additions
            if (
                mapping := mapping_by_account_and_key.get(
                    (account_for_side[action.side], action.track.key)
                )
            )
            is not None
        }

    def _resolve_additions(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        source_provider: SyncProvider,
        target_provider: SyncProvider,
    ) -> tuple[dict[int, ProviderTrack], tuple[int, ...], dict[int, tuple[ProviderTrack, ...]]]:
        resolutions = self._cached_resolutions(pair, plan)
        unresolved: list[int] = []
        candidates_by_index: dict[int, tuple[ProviderTrack, ...]] = {}
        by_destination_and_key: dict[tuple[Side, str], ProviderTrack | None] = {}
        candidates_by_destination_and_key: dict[tuple[Side, str], tuple[ProviderTrack, ...]] = {}
        lookups = 0
        for index, action in enumerate(plan.actions):
            if action.action is not ActionType.ADD_TRACK or index in resolutions:
                continue
            lookup_key = (action.side, action.track.key)
            provider = source_provider if action.side is Side.SOURCE else target_provider
            provider_track = ProviderTrack(
                provider_track_id=action.track.source_provider_track_id,
                title=action.track.title,
                artists=action.track.artists,
                duration_ms=action.track.duration_ms,
                isrc=action.track.isrc,
                occurrence_id=action.track.occurrence_id,
                position=action.track.position,
            )
            if lookup_key in by_destination_and_key:
                resolved = by_destination_and_key[lookup_key]
            else:
                lookups += 1
                if lookups > MAX_REVIEW_LOOKUPS:
                    raise ValueError(
                        f"review requires more than {MAX_REVIEW_LOOKUPS} provider searches; "
                        "split the playlist or establish a trusted baseline"
                    )
                resolved = provider.search_track(provider_track)
                by_destination_and_key[lookup_key] = resolved
            if resolved is None:
                unresolved.append(index)
                candidate_lookup = getattr(provider, "close_track_candidates", None)
                if callable(candidate_lookup):
                    candidates = candidates_by_destination_and_key.get(lookup_key)
                    if candidates is None:
                        candidates = tuple(candidate_lookup(provider_track))[:MAX_MANUAL_CANDIDATES]
                        candidates_by_destination_and_key[lookup_key] = candidates
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
        try:
            summary = json.loads(run.summary_json or "{}")
            unresolved_indices = tuple(
                int(value) for value in summary.get("unresolved_indices", ())
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            unresolved_indices = ()
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
                and existing.approval_expires_at is not None
                and _utc(existing.approval_expires_at) > now
                and existing.started_at is not None
                and _utc(existing.started_at) + REVIEW_REUSE_WINDOW > now
            ):
                token = secrets.token_urlsafe(32)
                existing.approval_token_hash = _token_hash(token)
                self.session.commit()
                return self._prepared_from_run(existing, token)

            latest = run_repo.latest_for_pair(pair.id)
            if (
                latest is not None
                and latest.status in {"preparing", "review_failed"}
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
            plan, baseline_record, baseline_upgrade = self._build_plan(pair, source, target)
            if len(plan.actions) > MAX_PLAN_ACTIONS:
                raise ValueError(
                    f"review contains more than {MAX_PLAN_ACTIONS} actions; split the playlist"
                )
            resolutions: dict[int, ProviderTrack] = {}
            unresolved_indices: tuple[int, ...] = ()
            candidate_options: dict[int, tuple[ProviderTrack, ...]] = {}
            if not baseline_upgrade and not plan.conflicts:
                resolutions, unresolved_indices, candidate_options = self._resolve_additions(
                    pair, plan, source_provider, target_provider
                )
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
            run.approval_expires_at = now + REVIEW_APPROVAL_TTL
            run.summary_json = json.dumps(
                {
                    "actions": len(plan.actions),
                    "conflicts": len(plan.conflicts),
                    "initial_sync": plan.initial_sync,
                    "policy": plan.initial_policy.value if plan.initial_policy else None,
                    "unresolved_indices": unresolved_indices,
                    "baseline_upgrade": baseline_upgrade,
                },
                sort_keys=True,
            )
            run.completed_at = now
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
                        "review_failed",
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
            persisted_plan = decode_plan(run.plan_json)
            if persisted_plan != plan:
                raise ReviewNotApplicable("the submitted plan is not the persisted review")
            if approval.plan_fingerprint != (run.plan_fingerprint or ""):
                raise DestructiveActionApprovalError("the approval does not match the review")
            from ops.sync.safety import validate_approval

            validate_approval(plan, approval)
            current_source, current_target, source_provider, target_provider = self._current_state(
                pair
            )
            if (
                playlist_state_hash(current_source) != run.source_state_hash
                or playlist_state_hash(current_target) != run.target_state_hash
            ):
                raise ValueError("provider state changed; discard the old review and review again")
            self._ensure_unambiguous_spotify_removals(
                plan,
                current_source,
                current_target,
                source_provider,
                target_provider,
            )
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
            resolutions = _decode_resolutions(run.resolution_json)

            def completed(index: int) -> None:
                action_repo.complete(journal[index])
                self.session.commit()
                lease.renew()

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
                pre_resolved_tracks=resolutions,
                on_action_completed=completed,
                on_track_resolved=lambda action, track: self._save_track_mapping(
                    pair.id,
                    pair.source_account_id
                    if action.side is Side.SOURCE
                    else pair.target_account_id,
                    track,
                    action.track.key,
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
                resulting_source, resulting_target, _, _ = self._current_state(pair)
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
