from ops.providers.types import ProviderTrack
from ops.sync.domain import ActionType, ReconciliationAction, ReconciliationPlan, Side, TrackState
from ops.sync.executor import SyncExecutor


class FakeProvider:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    def search_track(self, track: ProviderTrack) -> ProviderTrack:
        return ProviderTrack("resolved-track", track.title, track.artists)

    def add_tracks(self, playlist_id: str, tracks: list[ProviderTrack]) -> None:
        self.added.extend(track.provider_track_id for track in tracks)

    def remove_tracks(self, playlist_id: str, tracks: list[ProviderTrack]) -> None:
        self.removed.extend(track.provider_track_id for track in tracks)


def test_executor_preflights_and_applies_non_destructive_addition() -> None:
    provider = FakeProvider()
    plan = ReconciliationPlan(
        actions=(
            ReconciliationAction(
                side=Side.TARGET,
                action=ActionType.ADD_TRACK,
                track=TrackState("text:song|artist", "Song", ("Artist",), "source-track"),
                reason="test",
            ),
        ),
        conflicts=(),
    )

    SyncExecutor().apply(
        plan,
        source_provider=provider,
        target_provider=provider,
        source_playlist_id="source",
        target_playlist_id="target",
    )

    assert provider.added == ["resolved-track"]


def test_executor_can_skip_an_unresolved_track_after_review() -> None:
    class UnresolvedProvider(FakeProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
            return None if track.title == "Unavailable" else super().search_track(track)

    provider = UnresolvedProvider()
    plan = ReconciliationPlan(
        actions=(
            ReconciliationAction(
                Side.TARGET,
                ActionType.ADD_TRACK,
                TrackState("text:unavailable|artist", "Unavailable", ("Artist",), "source-one"),
                "test",
            ),
            ReconciliationAction(
                Side.TARGET,
                ActionType.ADD_TRACK,
                TrackState("text:available|artist", "Available", ("Artist",), "source-two"),
                "test",
            ),
        ),
        conflicts=(),
    )

    result = SyncExecutor().apply(
        plan,
        source_provider=provider,
        target_provider=provider,
        source_playlist_id="source",
        target_playlist_id="target",
        skip_unresolved=True,
    )

    assert result.skipped_indices == (0,)
    assert provider.added == ["resolved-track"]
