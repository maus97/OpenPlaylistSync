from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.domain import (
    ActionType,
    BaselineState,
    PlaylistState,
    Side,
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


def test_initial_sync_is_preview_only() -> None:
    plan = reconcile(None, baseline().source, baseline().target)

    assert plan.initial_sync is True
    assert plan.actions == ()
    assert plan.safe_to_apply is False


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
