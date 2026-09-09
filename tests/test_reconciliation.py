from dataclasses import replace
from itertools import product

import pytest

from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.domain import (
    ActionType,
    BaselineState,
    InitialSyncPolicy,
    PlaylistState,
    Side,
    SyncMode,
    reconcile,
)


def track(track_id: str, title: str) -> ProviderTrack:
    return ProviderTrack(provider_track_id=track_id, title=title, artists=("Artist",))


def playlist(provider: str, tracks: tuple[ProviderTrack, ...]) -> PlaylistState:
    provider_playlist = ProviderPlaylist(
        provider_playlist_id=f"{provider}:playlist-1",
        name="Shared playlist",
        tracks=tracks,
    )
    return PlaylistState.from_provider_playlist(provider_playlist)


def baseline() -> BaselineState:
    source = playlist("spotify", (track("s-one", "One"),))
    target = playlist("youtube_music", (track("y-one", "One"),))
    return BaselineState(source=source, target=target)


def test_initial_merge_is_non_destructive_and_safe_to_apply() -> None:
    plan = reconcile(None, baseline().source, baseline().target)

    assert plan.initial_sync is True
    assert plan.actions == ()
    assert plan.safe_to_apply is True


def test_initial_merge_adds_missing_tracks_to_both_sides() -> None:
    source = playlist("spotify", (track("s-one", "One"), track("s-two", "Two")))
    target = playlist("youtube_music", (track("y-one", "One"), track("y-three", "Three")))

    plan = reconcile(None, source, target)

    assert plan.initial_policy is InitialSyncPolicy.MERGE
    assert {(action.side, action.track.title) for action in plan.actions} == {
        (Side.SOURCE, "Three"),
        (Side.TARGET, "Two"),
    }
    assert not plan.destructive_actions


def test_duplicate_occurrence_addition_is_not_collapsed() -> None:
    source = playlist("spotify", (track("s-one", "One"), track("s-one-2", "One")))
    target = playlist("youtube_music", (track("y-one", "One"),))

    plan = reconcile(None, source, target)

    assert len(plan.actions) == 1
    assert plan.actions[0].side is Side.TARGET


def test_source_addition_is_proposed_for_target() -> None:
    current_source = playlist("spotify", (track("s-one", "One"), track("s-two", "Two")))
    plan = reconcile(baseline(), current_source, baseline().target)

    assert len(plan.actions) == 1
    assert plan.actions[0].side is Side.TARGET
    assert plan.actions[0].action is ActionType.ADD_TRACK
    assert plan.actions[0].track.title == "Two"
    assert plan.safe_to_apply is True


def test_target_addition_is_proposed_for_source() -> None:
    current_target = playlist("youtube_music", (track("y-one", "One"), track("y-two", "Two")))
    plan = reconcile(baseline(), baseline().source, current_target)

    assert len(plan.actions) == 1
    assert plan.actions[0].side is Side.SOURCE
    assert plan.actions[0].track.title == "Two"


def test_source_controlled_mode_never_writes_to_source() -> None:
    current_source = playlist("spotify", (track("s-one", "One"), track("s-two", "Two")))
    current_target = playlist("youtube_music", (track("y-one", "One"), track("y-three", "Three")))

    plan = reconcile(baseline(), current_source, current_target, mode=SyncMode.SOURCE_TO_TARGET)

    assert {(action.action, action.track.title) for action in plan.actions} == {
        (ActionType.ADD_TRACK, "Two"),
        (ActionType.REMOVE_TRACK, "Three"),
    }
    assert all(action.side is Side.TARGET for action in plan.actions)


def test_source_controlled_first_sync_only_adds_to_target() -> None:
    source = playlist("spotify", (track("s-one", "One"),))
    target = playlist("youtube_music", (track("y-two", "Two"),))

    plan = reconcile(None, source, target, mode=SyncMode.SOURCE_TO_TARGET)

    assert plan.initial_policy is InitialSyncPolicy.SOURCE_AUTHORITATIVE
    assert [(action.side, action.action, action.track.title) for action in plan.actions] == [
        (Side.TARGET, ActionType.ADD_TRACK, "One")
    ]


def test_source_controlled_mode_supports_youtube_music_to_spotify() -> None:
    source = playlist(
        "youtube_music", (track("youtube_music:one", "One"), track("youtube_music:two", "Two"))
    )
    target = playlist("spotify", (track("spotify:one", "One"),))
    before = BaselineState(
        source=playlist("youtube_music", (track("youtube_music:one", "One"),)),
        target=playlist("spotify", (track("spotify:one", "One"),)),
    )

    plan = reconcile(before, source, target, mode=SyncMode.SOURCE_TO_TARGET)

    assert [(action.side, action.action, action.track.title) for action in plan.actions] == [
        (Side.TARGET, ActionType.ADD_TRACK, "Two")
    ]


def test_one_sided_removal_is_destructive_and_requires_approval() -> None:
    current_source = playlist("spotify", ())
    plan = reconcile(baseline(), current_source, baseline().target)

    assert len(plan.destructive_actions) == 1
    assert plan.requires_approval is True
    assert plan.destructive_actions[0].side is Side.TARGET


def test_incompatible_changes_become_conflicts() -> None:
    # Stable ISRC identity makes this a metadata conflict rather than two
    # unrelated add/remove operations.
    conflict_baseline = BaselineState(
        source=PlaylistState.from_provider_playlist(
            ProviderPlaylist(
                provider_playlist_id="spotify:playlist-1",
                name="Shared playlist",
                tracks=(ProviderTrack("s-one", "One", ("Artist",), isrc="ISRC-1"),),
            )
        ),
        target=PlaylistState.from_provider_playlist(
            ProviderPlaylist(
                provider_playlist_id="youtube_music:playlist-1",
                name="Shared playlist",
                tracks=(ProviderTrack("y-one", "One", ("Artist",), isrc="ISRC-1"),),
            )
        ),
    )
    current_source = PlaylistState.from_provider_playlist(
        ProviderPlaylist(
            provider_playlist_id="spotify:playlist-1",
            name="Shared playlist",
            tracks=(ProviderTrack("s-one", "One (source edit)", ("Artist",), isrc="ISRC-1"),),
        )
    )
    current_target = PlaylistState.from_provider_playlist(
        ProviderPlaylist(
            provider_playlist_id="youtube_music:playlist-1",
            name="Shared playlist",
            tracks=(ProviderTrack("y-one", "One (target edit)", ("Artist",), isrc="ISRC-1"),),
        )
    )
    plan = reconcile(conflict_baseline, current_source, current_target)

    assert len(plan.conflicts) == 1
    assert plan.actions == ()
    assert plan.safe_to_apply is False


def test_same_change_on_both_sides_is_converged() -> None:
    current_source = playlist("spotify", (track("s-one", "One"), track("s-two", "Two")))
    current_target = playlist("youtube_music", (track("y-one", "One"), track("y-two", "Two")))
    plan = reconcile(baseline(), current_source, current_target)

    assert plan.actions == ()
    assert plan.conflicts == ()


@pytest.mark.parametrize("deleted_side", [Side.SOURCE, Side.TARGET])
def test_unequal_baseline_counts_remove_only_existing_destination_occurrences(
    deleted_side: Side,
) -> None:
    # An explicitly accepted baseline can contain more copies on one side.
    # Removing all of those copies must not delete the other occurrence twice.
    many = tuple(track(f"many-{index}", "One") for index in range(3))
    one = (track("only-destination-copy", "One"),)
    source_before = many if deleted_side is Side.SOURCE else one
    target_before = one if deleted_side is Side.SOURCE else many
    before = BaselineState(
        source=playlist("spotify", source_before),
        target=playlist("youtube_music", target_before),
    )
    source = playlist("spotify", () if deleted_side is Side.SOURCE else source_before)
    target = playlist("youtube_music", () if deleted_side is Side.TARGET else target_before)

    plan = reconcile(before, source, target)

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action is ActionType.REMOVE_TRACK
    assert action.side is (Side.TARGET if deleted_side is Side.SOURCE else Side.SOURCE)
    assert action.track.source_provider_track_id == "only-destination-copy"


@pytest.mark.parametrize("source_count,target_count", [(3, 1), (1, 3)])
def test_unequal_baseline_counts_do_not_remove_already_absent_tracks(
    source_count: int, target_count: int
) -> None:
    before = BaselineState(
        source=playlist("spotify", tuple(track(f"s-{i}", "One") for i in range(source_count))),
        target=playlist(
            "youtube_music", tuple(track(f"y-{i}", "One") for i in range(target_count))
        ),
    )

    plan = reconcile(before, playlist("spotify", ()), playlist("youtube_music", ()))

    assert plan.actions == ()
    assert plan.conflicts == ()


@pytest.mark.parametrize(
    "source_provider,target_provider", [("spotify", "youtube_music"), ("youtube_music", "spotify")]
)
def test_source_controlled_mode_restores_source_occurrences_after_target_edit(
    source_provider: str, target_provider: str
) -> None:
    source = playlist(source_provider, (track("source-one", "One"), track("source-two", "Two")))
    before = BaselineState(
        source=source,
        target=playlist(target_provider, (track("target-one", "One"), track("target-two", "Two"))),
    )
    edited_target = playlist(
        target_provider, (track("target-two", "Two"), track("target-three", "Three"))
    )

    plan = reconcile(before, source, edited_target, mode=SyncMode.SOURCE_TO_TARGET)

    assert [(a.side, a.action, a.track.title) for a in plan.actions] == [
        (Side.TARGET, ActionType.ADD_TRACK, "One"),
        (Side.TARGET, ActionType.REMOVE_TRACK, "Three"),
    ]
    # Applying that exact target-only repair converges even before the next
    # baseline is saved, so retrying the reconciliation cannot repeat a write.
    assert reconcile(before, source, before.target, mode=SyncMode.SOURCE_TO_TARGET).actions == ()


@pytest.mark.parametrize("mode", list(SyncMode))
def test_occurrence_reconciliation_converges_without_repeating_operations(mode: SyncMode) -> None:
    def copies(provider: str, count: int) -> PlaylistState:
        return playlist(provider, tuple(track(f"{provider}-{i}", "One") for i in range(count)))

    # Cover independently edited duplicate counts and intentionally asymmetric
    # accepted baselines, including an empty playlist on either side.
    for source_before, target_before, source_now, target_now in product(range(4), repeat=4):
        before = BaselineState(
            source=copies("spotify", source_before),
            target=copies("youtube_music", target_before),
        )
        source = copies("spotify", source_now)
        target = copies("youtube_music", target_now)
        plan = reconcile(before, source, target, mode=mode)
        if plan.conflicts:
            continue
        current = {Side.SOURCE: list(source.tracks), Side.TARGET: list(target.tracks)}
        for action in plan.actions:
            if action.action is ActionType.ADD_TRACK:
                current[action.side].append(action.track)
            else:
                # Also proves every removed occurrence exists exactly once.
                current[action.side].remove(action.track)
        repeated = reconcile(
            before,
            replace(source, tracks=tuple(current[Side.SOURCE])),
            replace(target, tracks=tuple(current[Side.TARGET])),
            mode=mode,
        )
        assert repeated.actions == ()
        assert repeated.conflicts == ()
