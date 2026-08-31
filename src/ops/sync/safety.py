"""Explicit safety checks for destructive synchronization plans."""

from dataclasses import dataclass

from ops.sync.domain import ReconciliationPlan


class DestructiveActionApprovalError(ValueError):
    """Raised when a destructive plan is not explicitly approved."""


@dataclass(frozen=True, slots=True)
class Approval:
    """A short-lived operator approval for one exact plan."""

    plan_fingerprint: str
    confirmation: str


def plan_fingerprint(plan: ReconciliationPlan) -> str:
    """Create a stable fingerprint for the displayed plan."""

    parts = [f"{action.side}:{action.action}:{action.track.key}" for action in plan.actions]
    parts.extend(
        f"conflict:{conflict.track_key}:{conflict.source_change}:{conflict.target_change}"
        for conflict in plan.conflicts
    )
    return "|".join(parts)


def validate_approval(plan: ReconciliationPlan, approval: Approval | None) -> None:
    """Reject conflicts, initial plans, stale plans, or missing confirmation."""

    if plan.conflicts:
        raise DestructiveActionApprovalError("conflicts must be resolved before applying a plan")
    if plan.initial_sync and plan.destructive_actions:
        raise DestructiveActionApprovalError("initial synchronization can never remove tracks")
    if not plan.requires_approval:
        return
    if approval is None or approval.confirmation != "APPLY DESTRUCTIVE CHANGES":
        raise DestructiveActionApprovalError("explicit destructive-action confirmation is required")
    if approval.plan_fingerprint != plan_fingerprint(plan):
        raise DestructiveActionApprovalError("the approval does not match the current plan")
