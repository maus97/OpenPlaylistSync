import pytest

from ops.sync.domain import ActionType, ReconciliationAction, ReconciliationPlan, Side, TrackState
from ops.sync.safety import (
    Approval,
    DestructiveActionApprovalError,
    plan_fingerprint,
    validate_approval,
)


def destructive_plan() -> ReconciliationPlan:
    return ReconciliationPlan(
        actions=(
            ReconciliationAction(
                side=Side.TARGET,
                action=ActionType.REMOVE_TRACK,
                track=TrackState("text:one|artist", "One", ("Artist",), "track-1"),
                reason="test",
            ),
        ),
        conflicts=(),
    )


def test_destructive_plan_requires_exact_confirmation() -> None:
    with pytest.raises(DestructiveActionApprovalError):
        validate_approval(destructive_plan(), None)

    approval = Approval(plan_fingerprint(destructive_plan()), "APPLY DESTRUCTIVE CHANGES")
    validate_approval(destructive_plan(), approval)


def test_stale_approval_is_rejected() -> None:
    approval = Approval("stale", "APPLY DESTRUCTIVE CHANGES")

    with pytest.raises(DestructiveActionApprovalError, match="does not match"):
        validate_approval(destructive_plan(), approval)
