"""Apply approved reconciliation plans through provider adapters."""

from collections.abc import Sequence
from typing import Protocol

from ops.providers.types import ProviderTrack
from ops.sync.domain import ActionType, ReconciliationAction, ReconciliationPlan, Side, TrackState
from ops.sync.safety import Approval, validate_approval


class SyncProvider(Protocol):
    """The provider operations required by the safety executor."""

    def search_track(self, track: ProviderTrack) -> ProviderTrack | None: ...

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None: ...

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None: ...


class PlanExecutionError(RuntimeError):
    """Raised when an approved plan cannot be fully prepared or applied."""


def _provider_track(track: TrackState) -> ProviderTrack:
    return ProviderTrack(
        provider_track_id=track.source_provider_track_id,
        title=track.title,
        artists=track.artists,
        duration_ms=track.duration_ms,
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
        approval: Approval | None = None,
    ) -> None:
        validate_approval(plan, approval)
        prepared: list[tuple[ReconciliationAction, ProviderTrack]] = []

        for action in plan.actions:
            provider = source_provider if action.side is Side.SOURCE else target_provider
            provider_track = _provider_track(action.track)
            if action.action is ActionType.ADD_TRACK:
                resolved = provider.search_track(provider_track)
                if resolved is None:
                    raise PlanExecutionError(f"track could not be resolved: {action.track.title}")
                provider_track = resolved
            prepared.append((action, provider_track))

        for action, provider_track in prepared:
            provider = source_provider if action.side is Side.SOURCE else target_provider
            playlist_id = source_playlist_id if action.side is Side.SOURCE else target_playlist_id
            if action.action is ActionType.ADD_TRACK:
                provider.add_tracks(playlist_id, [provider_track])
            else:
                provider.remove_tracks(playlist_id, [provider_track])
