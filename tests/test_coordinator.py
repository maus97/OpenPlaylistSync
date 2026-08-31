from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, SyncPair
from ops.providers.demo import mutate_demo_state, reset_demo_state
from ops.providers.factory import create_provider
from ops.storage.repositories import SyncActionRepository, SyncBaselineRepository, SyncRunRepository
from ops.sync.coordinator import SyncCoordinator
from ops.sync.domain import InitialSyncPolicy


def test_initial_merge_applies_additions_journals_actions_and_creates_baseline() -> None:
    reset_demo_state()
    mutate_demo_state("source_add")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="demo_spotify", external_account_id="source")
        target = ProviderAccount(provider_name="demo_youtube_music", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:demo-playlist",
            target_playlist_id="youtube_music:demo-playlist",
            initial_sync_policy=InitialSyncPolicy.MERGE.value,
        )
        session.add(pair)
        session.commit()

        coordinator = SyncCoordinator(session, Settings(), create_provider)
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
    engine.dispose()
    reset_demo_state()
