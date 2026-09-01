"""Canonical serialization for baselines, reviews, and exact state binding."""

import hashlib
import json

from ops.sync.domain import (
    ActionType,
    BaselineState,
    InitialSyncPolicy,
    PlaylistState,
    ReconciliationAction,
    ReconciliationConflict,
    ReconciliationPlan,
    Side,
    TrackState,
)


def _track_to_dict(track: TrackState) -> dict[str, object]:
    return {
        "key": track.key,
        "title": track.title,
        "artists": list(track.artists),
        "source_provider_track_id": track.source_provider_track_id,
        "duration_ms": track.duration_ms,
        "isrc": track.isrc,
        "occurrence_id": track.occurrence_id,
        "position": track.position,
    }


def _track_from_dict(track: dict[str, object]) -> TrackState:
    return TrackState(
        key=str(track["key"]),
        title=str(track["title"]),
        artists=tuple(str(artist) for artist in track["artists"]),
        source_provider_track_id=str(track["source_provider_track_id"]),
        duration_ms=track.get("duration_ms"),
        isrc=track.get("isrc"),
        occurrence_id=track.get("occurrence_id"),
        position=track.get("position"),
    )


def _playlist_to_dict(playlist: PlaylistState) -> dict[str, object]:
    return {
        "provider": playlist.provider,
        "playlist_id": playlist.playlist_id,
        "name": playlist.name,
        "snapshot_id": playlist.snapshot_id,
        "tracks": [_track_to_dict(track) for track in playlist.tracks],
    }


def _playlist_from_dict(payload: dict[str, object]) -> PlaylistState:
    tracks = tuple(_track_from_dict(track) for track in payload["tracks"])
    return PlaylistState(
        provider=str(payload["provider"]),
        playlist_id=str(payload["playlist_id"]),
        name=str(payload["name"]),
        tracks=tracks,
        snapshot_id=str(payload["snapshot_id"]) if payload.get("snapshot_id") else None,
    )


def encode_baseline(baseline: BaselineState) -> str:
    return json.dumps(
        {
            "source": _playlist_to_dict(baseline.source),
            "target": _playlist_to_dict(baseline.target),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def decode_baseline(value: str) -> BaselineState:
    payload = json.loads(value)
    return BaselineState(
        source=_playlist_from_dict(payload["source"]),
        target=_playlist_from_dict(payload["target"]),
    )


def encode_plan(plan: ReconciliationPlan) -> str:
    return json.dumps(
        {
            "actions": [
                {
                    "side": action.side.value,
                    "action": action.action.value,
                    "track": _track_to_dict(action.track),
                    "reason": action.reason,
                }
                for action in plan.actions
            ],
            "conflicts": [
                {
                    "track_key": conflict.track_key,
                    "source_change": conflict.source_change,
                    "target_change": conflict.target_change,
                    "reason": conflict.reason,
                }
                for conflict in plan.conflicts
            ],
            "initial_sync": plan.initial_sync,
            "initial_policy": plan.initial_policy.value if plan.initial_policy else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def decode_plan(value: str) -> ReconciliationPlan:
    payload = json.loads(value)
    return ReconciliationPlan(
        actions=tuple(
            ReconciliationAction(
                side=Side(action["side"]),
                action=ActionType(action["action"]),
                track=_track_from_dict(action["track"]),
                reason=str(action["reason"]),
            )
            for action in payload["actions"]
        ),
        conflicts=tuple(
            ReconciliationConflict(
                track_key=str(conflict["track_key"]),
                source_change=str(conflict["source_change"]),
                target_change=str(conflict["target_change"]),
                reason=str(conflict["reason"]),
            )
            for conflict in payload["conflicts"]
        ),
        initial_sync=bool(payload.get("initial_sync")),
        initial_policy=(
            InitialSyncPolicy(payload["initial_policy"]) if payload.get("initial_policy") else None
        ),
    )


def playlist_state_hash(playlist: PlaylistState) -> str:
    """Bind approval to the complete ordered provider state, including occurrence IDs."""

    payload = json.dumps(
        _playlist_to_dict(playlist),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
