"""Pure, occurrence-aware synchronization domain and reconciliation rules."""

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ops.providers.types import ProviderPlaylist, ProviderTrack

TRACK_IDENTITY_VERSION = 2


def normalize_text(value: str) -> str:
    """Normalize human metadata without discarding non-Latin scripts or symbols."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    folded = unicodedata.normalize("NFKC", without_marks).casefold()
    normalized = "".join(
        character if unicodedata.category(character)[0] in {"L", "N", "S"} else " "
        for character in folded
    )
    return re.sub(r"\s+", " ", normalized).strip()


def track_key(track: ProviderTrack) -> str:
    """Return the portable first-pass identity shared by both providers.

    Spotify often exposes an ISRC where YouTube Music does not. A textual key is
    deliberately the common reconciliation key; ISRC is retained as matching
    evidence and used to detect incompatible metadata edits.
    """

    title = normalize_text(track.title)
    artists = ",".join(normalize_text(artist) for artist in track.artists)
    if not title and not artists:
        fallback = hashlib.sha256(track.provider_track_id.encode("utf-8")).hexdigest()
        return f"opaque:{fallback}"
    return f"text:{title}|{artists}"


@dataclass(frozen=True, slots=True)
class TrackState:
    """A provider-neutral playlist occurrence with display and match evidence."""

    key: str
    title: str
    artists: tuple[str, ...]
    source_provider_track_id: str
    duration_ms: int | None = None
    isrc: str | None = None
    occurrence_id: str | None = None
    position: int | None = None

    @classmethod
    def from_provider_track(cls, track: ProviderTrack) -> "TrackState":
        return cls(
            key=track_key(track),
            title=track.title,
            artists=track.artists,
            source_provider_track_id=track.provider_track_id,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
            occurrence_id=track.occurrence_id,
            position=track.position,
        )


@dataclass(frozen=True, slots=True)
class PlaylistState:
    """A normalized playlist snapshot that preserves duplicate occurrences."""

    provider: str
    playlist_id: str
    name: str
    tracks: tuple[TrackState, ...]
    snapshot_id: str | None = None

    @classmethod
    def from_provider_playlist(cls, playlist: ProviderPlaylist) -> "PlaylistState":
        return cls(
            provider=playlist.provider_playlist_id.split(":", 1)[0]
            if ":" in playlist.provider_playlist_id
            else "unknown",
            playlist_id=playlist.provider_playlist_id,
            name=playlist.name,
            tracks=tuple(TrackState.from_provider_track(track) for track in playlist.tracks),
            snapshot_id=playlist.snapshot_id,
        )

    def by_key(self) -> dict[str, TrackState]:
        """Return a representative item for legacy callers.

        Reconciliation itself uses :meth:`grouped_by_key`, not this lossy helper.
        """

        return {key: tracks[0] for key, tracks in self.grouped_by_key().items()}

    def grouped_by_key(self) -> dict[str, tuple[TrackState, ...]]:
        grouped: dict[str, list[TrackState]] = defaultdict(list)
        for track in self.tracks:
            grouped[track.key].append(track)
        return {key: tuple(items) for key, items in grouped.items()}


@dataclass(frozen=True, slots=True)
class BaselineState:
    """The previous verified successful source and target snapshots."""

    source: PlaylistState
    target: PlaylistState


class Side(StrEnum):
    SOURCE = "source"
    TARGET = "target"


class ActionType(StrEnum):
    ADD_TRACK = "add_track"
    REMOVE_TRACK = "remove_track"


class InitialSyncPolicy(StrEnum):
    """Operator-selected convergence policy for a pair with no baseline."""

    MERGE = "merge"
    SOURCE_AUTHORITATIVE = "source_authoritative"
    TARGET_AUTHORITATIVE = "target_authoritative"
    ACCEPT_AS_IS = "accept_as_is"


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
    """A deterministic, reviewable plan with no provider side effects."""

    actions: tuple[ReconciliationAction, ...]
    conflicts: tuple[ReconciliationConflict, ...]
    initial_sync: bool = False
    initial_policy: InitialSyncPolicy | None = None

    @property
    def destructive_actions(self) -> tuple[ReconciliationAction, ...]:
        return tuple(action for action in self.actions if action.destructive)

    @property
    def requires_approval(self) -> bool:
        return bool(self.destructive_actions)

    @property
    def safe_to_apply(self) -> bool:
        return not self.conflicts


def _counts(playlist: PlaylistState) -> Counter[str]:
    return Counter(track.key for track in playlist.tracks)


def _changes(baseline: PlaylistState, current: PlaylistState) -> tuple[Counter[str], Counter[str]]:
    baseline_counts = _counts(baseline)
    current_counts = _counts(current)
    return current_counts - baseline_counts, baseline_counts - current_counts


def _metadata_conflict_keys(
    baseline: BaselineState, source: PlaylistState, target: PlaylistState
) -> tuple[set[str], list[ReconciliationConflict]]:
    """Detect conflicting edits to one shared ISRC even if title text changed."""

    def by_isrc(playlist: PlaylistState) -> dict[str, TrackState]:
        return {normalize_text(track.isrc): track for track in playlist.tracks if track.isrc}

    baseline_source = by_isrc(baseline.source)
    baseline_target = by_isrc(baseline.target)
    source_current = by_isrc(source)
    target_current = by_isrc(target)
    suppressed: set[str] = set()
    conflicts: list[ReconciliationConflict] = []
    isrcs = set(baseline_source) & set(baseline_target) & set(source_current) & set(target_current)
    for isrc in sorted(isrcs):
        source_before = baseline_source[isrc]
        target_before = baseline_target[isrc]
        source_after = source_current[isrc]
        target_after = target_current[isrc]
        source_changed = source_after.key != source_before.key
        target_changed = target_after.key != target_before.key
        if source_changed and target_changed and source_after.key != target_after.key:
            suppressed.update(
                {
                    source_before.key,
                    target_before.key,
                    source_after.key,
                    target_after.key,
                }
            )
            conflicts.append(
                ReconciliationConflict(
                    track_key=f"isrc:{isrc}",
                    source_change="metadata changed",
                    target_change="metadata changed",
                    reason="both sides changed the same ISRC differently",
                )
            )
    return suppressed, conflicts


def _initial_actions(
    source: PlaylistState, target: PlaylistState, policy: InitialSyncPolicy
) -> list[ReconciliationAction]:
    if policy is InitialSyncPolicy.ACCEPT_AS_IS:
        return []
    source_groups = source.grouped_by_key()
    target_groups = target.grouped_by_key()
    source_counts = _counts(source)
    target_counts = _counts(target)
    actions: list[ReconciliationAction] = []
    for key in sorted(set(source_counts) | set(target_counts)):
        source_count = source_counts[key]
        target_count = target_counts[key]
        if policy in (InitialSyncPolicy.MERGE, InitialSyncPolicy.SOURCE_AUTHORITATIVE):
            for index in range(max(source_count - target_count, 0)):
                actions.append(
                    ReconciliationAction(
                        side=Side.TARGET,
                        action=ActionType.ADD_TRACK,
                        track=source_groups[key][index],
                        reason="initial sync adds source item without deleting target items",
                    )
                )
        if policy in (InitialSyncPolicy.MERGE, InitialSyncPolicy.TARGET_AUTHORITATIVE):
            for index in range(max(target_count - source_count, 0)):
                actions.append(
                    ReconciliationAction(
                        side=Side.SOURCE,
                        action=ActionType.ADD_TRACK,
                        track=target_groups[key][index],
                        reason="initial sync adds target item without deleting source items",
                    )
                )
    return actions


def _sorted_actions(actions: Iterable[ReconciliationAction]) -> tuple[ReconciliationAction, ...]:
    return tuple(
        sorted(
            actions,
            key=lambda action: (
                action.side,
                action.action,
                action.track.key,
                action.track.position or 0,
            ),
        )
    )


def reconcile(
    baseline: BaselineState | None,
    source: PlaylistState,
    target: PlaylistState,
    *,
    initial_policy: InitialSyncPolicy = InitialSyncPolicy.MERGE,
) -> ReconciliationPlan:
    """Build a three-way plan without provider calls or writes.

    The first plan can only add items. Deletions are derived exclusively from a
    previous verified shared baseline.
    """

    if baseline is None:
        return ReconciliationPlan(
            actions=_sorted_actions(_initial_actions(source, target, initial_policy)),
            conflicts=(),
            initial_sync=True,
            initial_policy=initial_policy,
        )

    source_adds, source_removes = _changes(baseline.source, source)
    target_adds, target_removes = _changes(baseline.target, target)
    source_tracks = source.grouped_by_key()
    target_tracks = target.grouped_by_key()
    baseline_source_tracks = baseline.source.grouped_by_key()
    baseline_target_tracks = baseline.target.grouped_by_key()
    suppressed, conflicts = _metadata_conflict_keys(baseline, source, target)
    actions: list[ReconciliationAction] = []
    keys = sorted(set(source_adds) | set(source_removes) | set(target_adds) | set(target_removes))

    for key in keys:
        if key in suppressed:
            continue
        source_added = source_adds[key]
        source_removed = source_removes[key]
        target_added = target_adds[key]
        target_removed = target_removes[key]
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

        if source_added > target_added:
            for index in range(source_added - target_added):
                actions.append(
                    ReconciliationAction(
                        side=Side.TARGET,
                        action=ActionType.ADD_TRACK,
                        track=source_tracks[key][index],
                        reason="source added the track since the last successful baseline",
                    )
                )
        elif target_added > source_added:
            for index in range(target_added - source_added):
                actions.append(
                    ReconciliationAction(
                        side=Side.SOURCE,
                        action=ActionType.ADD_TRACK,
                        track=target_tracks[key][index],
                        reason="target added the track since the last successful baseline",
                    )
                )

        if source_removed > target_removed:
            target_occurrences = target_tracks.get(key, baseline_target_tracks.get(key, ()))
            for index in range(source_removed - target_removed):
                if target_occurrences:
                    actions.append(
                        ReconciliationAction(
                            side=Side.TARGET,
                            action=ActionType.REMOVE_TRACK,
                            track=target_occurrences[index % len(target_occurrences)],
                            reason="source removed the track since the last successful baseline",
                        )
                    )
        elif target_removed > source_removed:
            source_occurrences = source_tracks.get(key, baseline_source_tracks.get(key, ()))
            for index in range(target_removed - source_removed):
                if source_occurrences:
                    actions.append(
                        ReconciliationAction(
                            side=Side.SOURCE,
                            action=ActionType.REMOVE_TRACK,
                            track=source_occurrences[index % len(source_occurrences)],
                            reason="target removed the track since the last successful baseline",
                        )
                    )

    return ReconciliationPlan(actions=_sorted_actions(actions), conflicts=tuple(conflicts))
