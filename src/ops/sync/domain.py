"""Pure synchronization domain types and three-way reconciliation."""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ops.providers.types import ProviderPlaylist, ProviderTrack


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text.casefold()).strip()


def track_key(track: ProviderTrack) -> str:
    """Return a conservative cross-provider identity for a track."""

    if track.isrc:
        return f"isrc:{_normalize_text(track.isrc)}"
    artists = ",".join(_normalize_text(artist) for artist in track.artists)
    return f"text:{_normalize_text(track.title)}|{artists}"


@dataclass(frozen=True, slots=True)
class TrackState:
    """A provider-neutral track with its matching key and display metadata."""

    key: str
    title: str
    artists: tuple[str, ...]
    source_provider_track_id: str
    duration_ms: int | None = None
    isrc: str | None = None

    @classmethod
    def from_provider_track(cls, track: ProviderTrack) -> "TrackState":
        return cls(
            key=track_key(track),
            title=track.title,
            artists=track.artists,
            source_provider_track_id=track.provider_track_id,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
        )


@dataclass(frozen=True, slots=True)
class PlaylistState:
    """A normalized playlist snapshot from one provider."""

    provider: str
    playlist_id: str
    name: str
    tracks: tuple[TrackState, ...]

    @classmethod
    def from_provider_playlist(cls, playlist: ProviderPlaylist) -> "PlaylistState":
        return cls(
            provider=playlist.provider_playlist_id.split(":", 1)[0]
            if ":" in playlist.provider_playlist_id
            else "unknown",
            playlist_id=playlist.provider_playlist_id,
            name=playlist.name,
            tracks=tuple(TrackState.from_provider_track(track) for track in playlist.tracks),
        )

    def by_key(self) -> dict[str, TrackState]:
        return {track.key: track for track in self.tracks}


@dataclass(frozen=True, slots=True)
class BaselineState:
    """The previous successful source and target snapshots."""

    source: PlaylistState
    target: PlaylistState


class Side(StrEnum):
    SOURCE = "source"
    TARGET = "target"


class ActionType(StrEnum):
    ADD_TRACK = "add_track"
    REMOVE_TRACK = "remove_track"


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    """A proposed change to one side of a synchronization pair."""

    side: Side
    action: ActionType
    track: TrackState
    reason: str

    @property
    def destructive(self) -> bool:
        return self.action is ActionType.REMOVE_TRACK


@dataclass(frozen=True, slots=True)
class ReconciliationConflict:
    """A change that cannot be safely inferred from the baseline."""

    track_key: str
    source_change: str
    target_change: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """A deterministic, reviewable synchronization plan."""

    actions: tuple[ReconciliationAction, ...]
    conflicts: tuple[ReconciliationConflict, ...]
    initial_sync: bool = False

    @property
    def destructive_actions(self) -> tuple[ReconciliationAction, ...]:
        return tuple(action for action in self.actions if action.destructive)

    @property
    def requires_approval(self) -> bool:
        return bool(self.destructive_actions)

    @property
    def safe_to_apply(self) -> bool:
        return not self.initial_sync and not self.conflicts


def _changes(baseline: PlaylistState, current: PlaylistState) -> tuple[set[str], set[str]]:
    baseline_keys = set(baseline.by_key())
    current_keys = set(current.by_key())
    return current_keys - baseline_keys, baseline_keys - current_keys


def _sorted_tracks(states: Iterable[TrackState]) -> tuple[TrackState, ...]:
    return tuple(sorted(states, key=lambda track: track.key))


def reconcile(
    baseline: BaselineState | None,
    source: PlaylistState,
    target: PlaylistState,
) -> ReconciliationPlan:
    """Build a three-way plan without making any provider calls or writes."""

    if baseline is None:
        return ReconciliationPlan(actions=(), conflicts=(), initial_sync=True)

    source_adds, source_removes = _changes(baseline.source, source)
    target_adds, target_removes = _changes(baseline.target, target)
    source_tracks = source.by_key()
    target_tracks = target.by_key()
    baseline_source_tracks = baseline.source.by_key()
    baseline_target_tracks = baseline.target.by_key()

    actions: list[ReconciliationAction] = []
    conflicts: list[ReconciliationConflict] = []
    keys = sorted(source_adds | source_removes | target_adds | target_removes)

    for key in keys:
        source_added = key in source_adds
        source_removed = key in source_removes
        target_added = key in target_adds
        target_removed = key in target_removes

        if (source_added and target_removed) or (source_removed and target_added):
            conflicts.append(
                ReconciliationConflict(
                    track_key=key,
                    source_change="added" if source_added else "removed",
                    target_change="added" if target_added else "removed",
                    reason="both sides changed the same track in incompatible ways",
                )
            )
            continue

        if source_added and not target_added:
            actions.append(
                ReconciliationAction(
                    side=Side.TARGET,
                    action=ActionType.ADD_TRACK,
                    track=source_tracks[key],
                    reason="source added the track since the last successful baseline",
                )
            )
        elif target_added and not source_added:
            actions.append(
                ReconciliationAction(
                    side=Side.SOURCE,
                    action=ActionType.ADD_TRACK,
                    track=target_tracks[key],
                    reason="target added the track since the last successful baseline",
                )
            )
        elif source_removed and not target_removed:
            actions.append(
                ReconciliationAction(
                    side=Side.TARGET,
                    action=ActionType.REMOVE_TRACK,
                    track=baseline_target_tracks.get(key, baseline_source_tracks[key]),
                    reason="source removed the track since the last successful baseline",
                )
            )
        elif target_removed and not source_removed:
            actions.append(
                ReconciliationAction(
                    side=Side.SOURCE,
                    action=ActionType.REMOVE_TRACK,
                    track=baseline_source_tracks.get(key, baseline_target_tracks[key]),
                    reason="target removed the track since the last successful baseline",
                )
            )

    for key in sorted(set(baseline.source.by_key()) & set(baseline.target.by_key())):
        source_baseline = baseline.source.by_key()[key]
        target_baseline = baseline.target.by_key()[key]
        source_current = source.by_key().get(key)
        target_current = target.by_key().get(key)
        source_changed = source_current is not None and source_current != source_baseline
        target_changed = target_current is not None and target_current != target_baseline
        if source_changed and target_changed and source_current != target_current:
            conflicts.append(
                ReconciliationConflict(
                    track_key=key,
                    source_change="metadata changed",
                    target_change="metadata changed",
                    reason="both sides changed the same stable track differently",
                )
            )

    return ReconciliationPlan(
        actions=tuple(actions),
        conflicts=tuple(conflicts),
        initial_sync=False,
    )
