"""One durable, observable scheduler evaluation; never replay uncertain writes."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ops.models import SyncAction, SyncRun
from ops.providers.health import active_incidents, retry_pending, utc
from ops.storage.repositories import SyncRunRepository
from ops.sync.automatic import automation_binding, run_automatic_pair
from ops.sync.leases import acquire_pair_lease


def recover_interrupted_runs(session, pair):
    """The operation lease is proof of exclusion, not an in-memory running flag."""
    unfinished = list(
        session.scalars(
            select(SyncRun).where(
                SyncRun.pair_id == pair.id, SyncRun.status.in_(("preparing", "applying"))
            )
        )
    )
    if not unfinished:
        return
    lease = acquire_pair_lease(session, pair.id)
    try:
        for run in unfinished:
            session.refresh(run)
            if run.status not in {"preparing", "applying"}:
                continue
            writes_possible = (
                run.status == "applying"
                and session.scalar(
                    select(SyncAction.id).where(SyncAction.run_id == run.id).limit(1)
                )
                is not None
            )
            SyncRunRepository(session).finish(
                run,
                "failed" if writes_possible else "interrupted",
                json.dumps(
                    {
                        "category": "internal",
                        "error_type": "InterruptedRun",
                        "error": "Interrupted with possible writes; review before continuing."
                        if writes_possible
                        else "Interrupted before writes; automatic retry is safe.",
                    }
                ),
            )
        session.commit()
    finally:
        lease.release()


def evaluate_pair(coordinator, pair):
    """Return a safe reason for every skip; each caller owns a fresh DB session."""
    session, settings = coordinator.session, coordinator.settings
    now = datetime.now(UTC)
    latest = SyncRunRepository(session).latest_for_pair(pair.id)
    pair.automatic_checked_at = now
    pair.automatic_next_at = now + timedelta(minutes=settings.sync_interval_minutes)
    if retry_pending(session, pair, latest):
        incidents = active_incidents(session, pair)
        if incidents:
            incident = max(incidents, key=lambda i: utc(i.retry_at))
            pair.automatic_next_at = max(utc(pair.automatic_next_at), utc(incident.retry_at))
            outcome = f"Waiting for {incident.category} retry (account #{incident.account_id})"
        else:
            pair.automatic_next_at = max(
                utc(pair.automatic_next_at),
                utc(latest.completed_at or latest.started_at) + timedelta(hours=1),
            )
            outcome = "Waiting for legacy authorization retry; verify both connections"
    else:
        session.commit()
        recover_interrupted_runs(session, pair)
        pair.automatic_attempted_at = now
        pair.automatic_outcome = "Running"
        session.commit()
        if automation_binding(pair) in settings.automatic_sync_bindings:
            outcome = run_automatic_pair(coordinator, pair)
        else:
            coordinator.preview(pair)
            outcome = "Scheduled review completed; automatic changes not authorized"
        if outcome == "up to date" or outcome.startswith("applied"):
            pair.automatic_succeeded_at = datetime.now(UTC)
    pair.automatic_outcome = outcome
    session.commit()
    return outcome
