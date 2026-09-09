"""Explicit pair-scoped automation using the normal state-bound apply pipeline."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ops.models import SyncPair, SyncRun
from ops.storage.repositories import SyncBaselineRepository
from ops.sync.coordinator import SyncCoordinator
from ops.sync.domain import Side
from ops.sync.safety import Approval, plan_fingerprint
from ops.sync.serialization import decode_baseline


def automation_binding(pair: SyncPair) -> str:
    """Consent cannot silently transfer to a changed account or playlist pair."""
    identity = [
        pair.id,
        str(pair.created_at),
        pair.source_account_id,
        pair.target_account_id,
        pair.source_playlist_id,
        pair.target_playlist_id,
        pair.sync_mode,
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def authorization_retry_pending(run: SyncRun | None) -> bool:
    """Back off failed authorization without permanently disabling recovery."""
    if run is None or run.status != "authorization_required":
        return False
    failed_at = run.completed_at or run.started_at
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=UTC)
    return datetime.now(UTC) < failed_at + timedelta(hours=1)


def run_automatic_pair(coordinator: SyncCoordinator, pair: SyncPair) -> str:
    """Fail closed on first sync, uncertain prior writes, and unusually large removals."""
    if (
        not pair.enabled
        or automation_binding(pair) not in coordinator.settings.automatic_sync_bindings
    ):
        return "disabled"
    latest = coordinator.session.scalar(
        select(SyncRun)
        .where(SyncRun.pair_id == pair.id)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    if authorization_retry_pending(latest):
        return "authorization needs attention"
    baseline = SyncBaselineRepository(coordinator.session).latest_for_pair(pair.id)
    if baseline is None:
        return "manual first sync required"
    uncertain = coordinator.session.scalar(
        select(SyncRun.id)
        .where(
            SyncRun.pair_id == pair.id,
            SyncRun.status.in_(("failed", "partially_applied", "applying")),
            SyncRun.started_at >= baseline.synchronized_at,
        )
        .limit(1)
    )
    if uncertain is not None:
        return "manual recovery required after incomplete apply"
    review = coordinator.prepare_review(pair)
    if review.baseline_upgrade_required or review.plan.initial_sync:
        return "manual baseline review required"
    if review.plan.conflicts or review.unresolved_actions:
        return "manual matching or conflict review required"
    if not review.plan.actions:
        return "up to date"
    before = decode_baseline(baseline.snapshot_json)
    for side, state in ((Side.SOURCE, before.source), (Side.TARGET, before.target)):
        removed = sum(a.side is side for a in review.plan.destructive_actions)
        if removed and (
            removed >= len(state.tracks) or removed > min(10, max(1, len(state.tracks) // 4))
        ):
            return "manual review required for bulk removals"
    coordinator.apply(
        pair,
        review.plan,
        Approval(
            plan_fingerprint(review.plan),
            "",
            review_id=review.review_id,
            token=review.approval_token,
        ),
    )
    return "applied"
