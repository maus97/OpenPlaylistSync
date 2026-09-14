"""Failure containment and real background-timer tests with synthetic data only."""

from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from test_provider_health import context as context
from test_provider_health import failure
from test_sync_roundtrip import apply_review
from test_sync_roundtrip import pair_context as pair_context

from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, SyncAction, SyncPair, SyncRun
from ops.providers.base import RateLimited
from ops.providers.health import retry_pending, utc
from ops.scheduler import SchedulerService
from ops.sync.leases import PairLeaseLost, PairOperationBusy, acquire_pair_lease
from ops.sync.scheduling import recover_interrupted_runs


def test_real_scheduler_runs_again_after_exception_and_keeps_future_job():
    completed = Event()
    attempts = []

    def job():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("synthetic transient failure")
        completed.set()

    service = SchedulerService(Settings(scheduler_enabled=True, sync_interval_minutes=10), job)
    service.start()
    try:
        service.scheduler.reschedule_job("synchronization-tick", trigger="interval", seconds=0.1)
        assert completed.wait(3)
        snapshot = service.snapshot()
        assert len(attempts) >= 2
        assert snapshot["healthy"]
        assert snapshot["next_tick"] > datetime.now(UTC) - timedelta(seconds=1)
        assert snapshot["last_started_at"]
        assert service.settings.sync_interval_minutes == 10
    finally:
        service.shutdown()


def test_heartbeat_detects_stall_not_provider_failure_and_watchdog_repairs_timer():
    service = SchedulerService(
        Settings(scheduler_enabled=True, sync_interval_minutes=10), lambda: False
    )
    service.start()
    try:
        service._tick()
        assert service.snapshot()["healthy"]
        assert service.snapshot()["last_error"]
        service._last_progress -= 1400
        assert not service.snapshot()["healthy"]
        service._tick()
        assert service.snapshot()["healthy"]
        service.scheduler.remove_job("synchronization-tick")
        assert not service.snapshot()["healthy"]
        service._watchdog()
        assert service.snapshot()["healthy"]
        service.scheduler.pause_job("synchronization-tick")
        service._watchdog()
        assert service.snapshot()["next_tick"]
        assert len(service.scheduler.get_jobs()) == 2
    finally:
        service.shutdown()


def test_live_job_cannot_be_queued_again_and_disabled_scheduler_is_healthy():
    service = SchedulerService(
        Settings(scheduler_enabled=True, sync_interval_minutes=10), lambda: None
    )
    service.start()
    try:
        service._busy_since = 1.0
        assert not service.request_check()
        service._busy_since = None
        service.reconfigure(Settings(scheduler_enabled=False))
        assert service.snapshot()["healthy"]
        assert not service.request_check()
    finally:
        service.shutdown()


def test_pair_exception_and_db_enumeration_failure_do_not_stop_future_cycles(tmp_path, monkeypatch):
    from ops import main

    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = Settings(
        scheduler_enabled=True, credential_encryption_key=Fernet.generate_key().decode()
    )
    with factory() as s:
        a = ProviderAccount(provider_name="spotify", external_account_id="synthetic")
        s.add(a)
        s.flush()
        for i in range(2):
            s.add(
                SyncPair(
                    source_account_id=a.id,
                    target_account_id=a.id,
                    source_playlist_id=str(i),
                    target_playlist_id="target",
                )
            )
        s.commit()
    calls = []

    class Coordinator:
        def __init__(self, session, config, _):
            self.session, self.settings = session, config

        def preview(self, pair):
            calls.append(pair.id)
            if pair.id == 1 and len(calls) == 1:
                # Leave a failed SQLAlchemy transaction, not just a clean Python error.
                self.session.add(ProviderAccount(provider_name=None, external_account_id="bad"))
                self.session.flush()

    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "load_app_settings", lambda _: settings)
    monkeypatch.setattr(main, "SyncCoordinator", Coordinator)
    assert main.run_scheduled_sync()
    assert calls == [1, 2]
    assert main.run_scheduled_sync()
    assert calls == [1, 2, 1, 2]
    with factory() as s:
        for p in s.scalars(select(SyncPair)):
            assert p.automatic_attempted_at
            assert p.automatic_next_at
            assert p.automatic_lock_token is None
    monkeypatch.setattr(
        main, "load_app_settings", lambda _: (_ for _ in ()).throw(RuntimeError("DB offline"))
    )
    assert not main.run_scheduled_sync()
    monkeypatch.setattr(main, "load_app_settings", lambda _: settings)
    assert main.run_scheduled_sync()
    assert calls[-2:] == [1, 2]
    engine.dispose()


@pytest.mark.parametrize(
    "status,with_action,expected",
    [
        ("preparing", False, "interrupted"),
        ("applying", False, "interrupted"),
        ("applying", True, "failed"),
    ],
)
def test_interrupted_run_recovers_only_when_safe(context, status, with_action, expected):
    s, _, pair = context
    run = SyncRun(pair_id=pair.id, status=status)
    s.add(run)
    s.flush()
    if with_action:
        s.add(
            SyncAction(
                run_id=run.id,
                ordinal=0,
                provider_name="spotify",
                operation="add_track",
                track_key="synthetic",
                status="planned",
            )
        )
    pair.operation_lock_token = "abandoned"
    pair.operation_lock_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    s.commit()
    recover_interrupted_runs(s, pair)
    s.refresh(run)
    assert run.status == expected
    assert run.completed_at
    assert s.get(SyncPair, pair.id, populate_existing=True).operation_lock_token is None


def test_active_operation_and_automatic_leases_exclude_duplicates(context):
    s, _, pair = context
    run = SyncRun(pair_id=pair.id, status="preparing")
    s.add(run)
    s.commit()
    lease = acquire_pair_lease(s, pair.id)
    with pytest.raises(PairOperationBusy):
        recover_interrupted_runs(s, pair)
    assert run.status == "preparing"
    lease.release()
    auto = acquire_pair_lease(s, pair.id, kind="automatic")
    with pytest.raises(PairOperationBusy):
        acquire_pair_lease(s, pair.id, kind="automatic")
    auto.release()


def test_expired_owner_cannot_resume_and_old_release_cannot_unlock_new_job(context):
    s, _, pair = context
    old = acquire_pair_lease(s, pair.id, duration=timedelta(seconds=-1))
    with pytest.raises(PairLeaseLost):
        old.renew()
    current = acquire_pair_lease(s, pair.id)
    old.release()
    with pytest.raises(PairOperationBusy):
        acquire_pair_lease(s, pair.id)
    current.release()


def test_persisted_rate_limit_expires_across_restart_with_naive_sqlite_time(context):
    s, accounts, pair = context
    run = failure(s, accounts[1], pair, RateLimited(30))
    from ops.models import ProviderIncident

    incident = s.scalar(select(ProviderIncident))
    assert 25 <= (utc(incident.retry_at) - datetime.now(UTC)).total_seconds() <= 30
    with Session(s.get_bind()) as restarted:
        assert retry_pending(
            restarted, restarted.get(SyncPair, pair.id), restarted.get(SyncRun, run.id)
        )
    incident.retry_at = (datetime.now(UTC) - timedelta(seconds=1)).replace(tzinfo=None)
    s.commit()
    assert not retry_pending(s, pair, run)


def test_health_endpoint_reports_scheduler_stall_without_calling_providers(tmp_path, monkeypatch):
    from test_security_boundaries import _isolated_client

    client, _, _ = _isolated_client(tmp_path, monkeypatch)

    class Dead:
        def snapshot(self):
            return {"healthy": False}

    client.app.state.scheduler = Dead()
    assert client.get("/healthz").status_code == 503
    # Diagnostics/control are not public endpoints.
    assert client.get("/system/scheduler", follow_redirects=False).status_code == 303
    assert client.post("/system/scheduler/check", follow_redirects=False).status_code == 303


def test_automatic_success_and_later_noop_update_durable_diagnostics(pair_context, monkeypatch):
    from contextlib import nullcontext

    from ops import main
    from ops.providers.types import ProviderTrack
    from ops.sync.automatic import automation_binding

    s, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    coordinator.settings.scheduler_enabled = True
    youtube.tracks.append(ProviderTrack("youtube_music:new", "Synthetic", ("Artist",)))
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(s))
    monkeypatch.setattr(main, "load_app_settings", lambda _: coordinator.settings)
    monkeypatch.setattr(main, "SyncCoordinator", lambda *_: coordinator)
    assert main.run_scheduled_sync()
    s.refresh(pair)
    assert pair.automatic_outcome == "applied"
    assert pair.automatic_attempted_at and pair.automatic_succeeded_at
    assert pair.automatic_next_at and spotify.writes == 1
    assert main.run_scheduled_sync()
    s.refresh(pair)
    assert pair.automatic_outcome == "up to date"
    assert pair.automatic_lock_token is None
    assert spotify.writes == 1


def test_best_available_match_outcome_is_successful_and_does_not_hold_next_cycle(
    pair_context, monkeypatch
):
    from ops.sync import scheduling
    from ops.sync.automatic import automation_binding

    s, pair, coordinator, _, _ = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    monkeypatch.setattr(
        scheduling,
        "run_automatic_pair",
        lambda *_: "applied; 2 best-available matches recorded",
    )

    first = scheduling.evaluate_pair(coordinator, pair)
    first_success = pair.automatic_succeeded_at
    second = scheduling.evaluate_pair(coordinator, pair)

    assert first == second == "applied; 2 best-available matches recorded"
    assert first_success is not None
    assert pair.automatic_succeeded_at is not None
    assert pair.automatic_outcome.startswith("applied")
    assert pair.automatic_next_at is not None
    assert not pair.automatic_outcome.startswith("manual")


def test_apply_preflight_network_error_remains_retryable_without_uncertain_write_hold(pair_context):
    from ops.providers.errors import NetworkFailure
    from ops.providers.types import ProviderTrack
    from ops.storage.repositories import SyncRunRepository

    s, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    spotify.tracks.append(ProviderTrack("spotify:new", "Synthetic", ("Artist",)))
    review = coordinator.prepare_review(pair)
    original = youtube.get_playlist
    youtube.get_playlist = lambda _: (_ for _ in ()).throw(NetworkFailure("temporary"))
    with pytest.raises(NetworkFailure):
        apply_review(coordinator, pair, review)
    run = SyncRunRepository(s).latest_for_pair(pair.id)
    assert run.status == "review_failed"
    assert not list(s.scalars(select(SyncAction).where(SyncAction.run_id == run.id)))
    assert spotify.writes == youtube.writes == 0
    youtube.get_playlist = original
