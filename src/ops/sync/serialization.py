"""JSON serialization for baseline snapshots."""

import json

from ops.sync.domain import BaselineState, PlaylistState, TrackState


def _playlist_to_dict(playlist: PlaylistState) -> dict[str, object]:
    return {
        "provider": playlist.provider,
        "playlist_id": playlist.playlist_id,
        "name": playlist.name,
        "tracks": [
            {
                "key": track.key,
                "title": track.title,
                "artists": list(track.artists),
                "source_provider_track_id": track.source_provider_track_id,
                "duration_ms": track.duration_ms,
                "isrc": track.isrc,
                "occurrence_id": track.occurrence_id,
                "position": track.position,
            }
            for track in playlist.tracks
        ],
    }


def _playlist_from_dict(payload: dict[str, object]) -> PlaylistState:
    tracks = tuple(
        TrackState(
            key=str(track["key"]),
            title=str(track["title"]),
            artists=tuple(str(artist) for artist in track["artists"]),
            source_provider_track_id=str(track["source_provider_track_id"]),
            duration_ms=track.get("duration_ms"),
            isrc=track.get("isrc"),
            occurrence_id=track.get("occurrence_id"),
            position=track.get("position"),
        )
        for track in payload["tracks"]
    )
    return PlaylistState(
        provider=str(payload["provider"]),
        playlist_id=str(payload["playlist_id"]),
        name=str(payload["name"]),
        tracks=tracks,
    )


def encode_baseline(baseline: BaselineState) -> str:
    return json.dumps(
        {
            "source": _playlist_to_dict(baseline.source),
            "target": _playlist_to_dict(baseline.target),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_baseline(value: str) -> BaselineState:
    payload = json.loads(value)
    return BaselineState(
        source=_playlist_from_dict(payload["source"]),
        target=_playlist_from_dict(payload["target"]),
    )
