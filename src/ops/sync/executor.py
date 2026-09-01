"""Apply approved reconciliation plans through provider adapters."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ops.providers.types import ProviderTrack
from ops.sync.domain import ActionType, ReconciliationAction, ReconciliationPlan, Side, TrackState
from ops.sync.safety import Approval, validate_approval


class SyncProvider(Protocol):
    """The provider operations required by the safety executor."""

    name: str

    def get_playlist(self, playlist_id: str): ...

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None: ...

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str | None: ...

    def remove_tracks(
        self,
        playlist_id: str,
        tracks: Sequence[ProviderTrack],
        *,
        snapshot_id: str | None = None,
    ) -> str | None: ...


class PlanExecutionError(RuntimeError):
    """Raised when an approved plan cannot be fully prepared or applied."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The exact actions applied or deliberately skipped after operator review."""

    skipped_indices: tuple[int, ...] = ()


def _provider_track(track: TrackState) -> ProviderTrack:
    return ProviderTrack(
        provider_track_id=track.source_provider_track_id,
        title=track.title,
        artists=track.artists,
        duration_ms=track.duration_ms,
        isrc=track.isrc,
        occurrence_id=track.occurrence_id,
        position=track.position,
    )


class SyncExecutor:
    """Preflight and apply one exact plan; no action occurs before preflight passes."""

    def apply(
        self,
        plan: ReconciliationPlan,
        *,
        source_provider: SyncProvider,
        target_provider: SyncProvider,
        source_playlist_id: str,
        target_playlist_id: str,
        source_snapshot_id: str | None = None,
        target_snapshot_id: str | None = None,
        approval: Approval | None = None,
        skip_unresolved: bool = False,
        pre_resolved_tracks: Mapping[int, ProviderTrack] | None = None,
        on_action_completed: Callable[[int], None] | None = None,
        on_track_resolved: Callable[[ReconciliationAction, ProviderTrack], None] | None = None,
    ) -> ExecutionResult:
        validate_approval(plan, approval)
        prepared: list[tuple[int, ReconciliationAction, ProviderTrack]] = []
        skipped_indices: list[int] = []
        snapshot_ids = {
            Side.SOURCE: source_snapshot_id,
            Side.TARGET: target_snapshot_id,
        }

        for index, action in enumerate(plan.actions):
            provider = source_provider if action.side is Side.SOURCE else target_provider
            provider_track = _provider_track(action.track)
            if action.action is ActionType.ADD_TRACK:
                resolved = (pre_resolved_tracks or {}).get(index)
                if resolved is None:
                    resolved = provider.search_track(provider_track)
                if resolved is None:
                    if skip_unresolved:
                        skipped_indices.append(index)
                        continue
                    raise PlanExecutionError(f"track could not be resolved: {action.track.title}")
                provider_track = resolved
            prepared.append((index, action, provider_track))

        for index, action, provider_track in prepared:
            provider = source_provider if action.side is Side.SOURCE else target_provider
            playlist_id = source_playlist_id if action.side is Side.SOURCE else target_playlist_id
            if action.action is ActionType.ADD_TRACK:
                new_snapshot = provider.add_tracks(playlist_id, [provider_track])
                if on_track_resolved is not None:
                    on_track_resolved(action, provider_track)
            else:
                new_snapshot = provider.remove_tracks(
                    playlist_id,
                    [provider_track],
                    snapshot_id=snapshot_ids[action.side],
                )
            if new_snapshot:
                snapshot_ids[action.side] = new_snapshot
            if on_action_completed is not None:
                on_action_completed(index)
        return ExecutionResult(skipped_indices=tuple(skipped_indices))
