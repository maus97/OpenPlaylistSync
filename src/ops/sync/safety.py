"""Explicit safety checks for destructive synchronization plans."""

import hashlib
from dataclasses import dataclass

from ops.sync.domain import ReconciliationPlan
from ops.sync.serialization import encode_plan


class DestructiveActionApprovalError(ValueError):
    """Raised when a destructive plan is not explicitly approved."""


@dataclass(frozen=True, slots=True)
class Approval:
    """A short-lived operator approval for one exact plan."""

    plan_fingerprint: str
    confirmation: str
    review_id: int | None = None
    token: str = ""


def plan_fingerprint(plan: ReconciliationPlan) -> str:
    """Create a stable fingerprint for the displayed plan."""

    payload = encode_plan(plan).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_approval(plan: ReconciliationPlan, approval: Approval | None) -> None:
    """Reject conflicts, initial plans, stale plans, or missing confirmation."""

    if plan.conflicts:
        raise DestructiveActionApprovalError("conflicts must be resolved before applying a plan")
    if plan.initial_sync and plan.destructive_actions:
        raise DestructiveActionApprovalError("initial synchronization can never remove tracks")
    if not plan.actions:
        return
    if approval is None or approval.plan_fingerprint != plan_fingerprint(plan):
        raise DestructiveActionApprovalError("the approval does not match the current plan")
    if plan.requires_approval and approval.confirmation != "APPLY DESTRUCTIVE CHANGES":
        raise DestructiveActionApprovalError("explicit destructive-action confirmation is required")
