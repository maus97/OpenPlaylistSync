from collections.abc import Sequence

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, SyncPair
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.storage.repositories import SyncActionRepository, SyncBaselineRepository, SyncRunRepository
from ops.sync.coordinator import SyncCoordinator
from ops.sync.domain import InitialSyncPolicy


class InMemoryProvider:
    """Small provider fake used to exercise the real coordinator seam."""

    def __init__(self, provider: str, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        self.provider = provider
        self.playlist_id = playlist_id
        self.tracks = list(tracks)

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        assert playlist_id == self.playlist_id
        return ProviderPlaylist(playlist_id, "Test playlist", tuple(self.tracks))

    def search_track(self, track: ProviderTrack) -> ProviderTrack:
        title = (
            f"{track.artists[0]} - {track.title} (Official Video)"
            if self.provider == "youtube_music"
            else track.title
        )
        return ProviderTrack(f"{self.provider}:resolved-{len(self.tracks)}", title, track.artists)

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        assert playlist_id == self.playlist_id
        self.tracks.extend(tracks)

    def remove_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        assert playlist_id == self.playlist_id
        for track in tracks:
            self.tracks.remove(track)


def test_initial_merge_applies_additions_journals_actions_and_creates_baseline() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="test_source", external_account_id="source")
        target = ProviderAccount(provider_name="test_target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source-playlist",
            target_playlist_id="youtube_music:target-playlist",
            initial_sync_policy=InitialSyncPolicy.MERGE.value,
        )
        session.add(pair)
        session.commit()

        providers = {
            source.id: InMemoryProvider(
                "spotify",
                "spotify:source-playlist",
                [ProviderTrack("spotify:track-1", "Song", ("Artist",))],
            ),
            target.id: InMemoryProvider("youtube_music", "youtube_music:target-playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        plan = coordinator.preview(pair)
        assert plan.initial_sync
        assert len(plan.actions) == 1

        coordinator.apply(pair, plan)

        baseline = SyncBaselineRepository(session).latest_for_pair(pair.id)
        assert baseline is not None
        run = SyncRunRepository(session).recent(1)[0]
        assert run.status == "applied"
        assert [action.status for action in SyncActionRepository(session).for_run(run.id)] == [
            "completed"
        ]
        providers[source.id].tracks.clear()
        removal_plan = coordinator.preview(pair)
        assert len(removal_plan.actions) == 1
        assert removal_plan.actions[0].action.value == "remove_track"
    engine.dispose()
