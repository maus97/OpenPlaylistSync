from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, SyncPair, SyncRun
from ops.storage.repositories import SyncRunRepository
from ops.sync.automatic import authorization_retry_pending, automation_binding, run_automatic_pair
from ops.sync.coordinator import SyncCoordinator


@pytest.mark.parametrize("age,pending", [(0, True), (59, True), (61, False)])
def test_authorization_backoff_is_bounded(age, pending):
    failed_at = datetime.now(UTC) - timedelta(minutes=age)
    run = SyncRun(
        pair_id=1, status="authorization_required", started_at=failed_at.replace(tzinfo=None)
    )
    assert authorization_retry_pending(run) is pending


def test_automatic_pair_does_not_retry_immediately_after_authorization_failure() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        run = SyncRunRepository(session).start(pair_id=pair.id)
        SyncRunRepository(session).finish(run, "authorization_required")
        session.commit()
        coordinator = SyncCoordinator(
            session,
            Settings(
                credential_encryption_key=Fernet.generate_key().decode("ascii"),
                automatic_sync_bindings=[automation_binding(pair)],
            ),
            lambda *_: (_ for _ in ()).throw(AssertionError("provider should not be called")),
        )

        assert run_automatic_pair(coordinator, pair) == "provider retry pending"
    engine.dispose()
