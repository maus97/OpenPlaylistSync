"""Structured provider failures and allowlisted diagnostics (never response text)."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ops.providers.base import (
    AuthorizationRequired,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    TrackUnavailable,
)


class PermissionDenied(ProviderError):
    category = "permissions"


class ReadOnlyResource(PermissionDenied):
    category = "read_only"


class ResourceMissing(ProviderError):
    category = "missing_resource"


class NetworkFailure(ProviderUnavailable):
    category = "network"


MESSAGES = {
    "authentication": "Authorization could not be renewed. Reconnect this service.",
    "permissions": "Access is not permitted. Check the granted scopes and playlist sharing.",
    "read_only": "This playlist cannot be modified. Use a writable target or Follow source.",
    "missing_resource": "The playlist or resource is unavailable or was deleted.",
    "rate_limit": "The service reached its request limit. OPS will retry after the wait.",
    "temporary": "The service is temporarily unavailable. OPS will retry automatically.",
    "network": "The service could not be reached. OPS will retry automatically.",
    "track_unavailable": "This recording is unavailable on the destination service.",
    "internal": "The operation failed unexpectedly. Check the application diagnostics.",
}


def category(exc: BaseException) -> str:
    if getattr(exc, "category", None) in MESSAGES:
        return exc.category
    for cls, name in (
        (AuthorizationRequired, "authentication"),
        (RateLimited, "rate_limit"),
        (ProviderUnavailable, "temporary"),
        (TrackUnavailable, "track_unavailable"),
    ):
        if isinstance(exc, cls):
            return name
    return "internal"


def diagnostic(exc: BaseException) -> dict:
    kind = category(exc)
    return {
        "error": MESSAGES[kind],
        "category": kind,
        "provider": getattr(exc, "provider", None),
        "account_id": getattr(exc, "account_id", None),
        "operation": getattr(exc, "operation", None),
        "error_type": type(exc).__name__,
        "incident_id": getattr(exc, "incident_id", None),
    }


def retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(1, int(value))
    except ValueError:
        try:
            return max(1, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def http_failure(
    status: int,
    *,
    reason: str | None = None,
    retry: str | None = None,
    write: bool = False,
    token: bool = False,
) -> ProviderError:
    reason = reason if isinstance(reason, str) else None
    if status == 429 or reason in {
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }:
        return RateLimited(retry_after(retry))
    if status >= 500:
        return ProviderUnavailable(MESSAGES["temporary"])
    if token and reason in {"invalid_client", "unauthorized_client", "invalid_scope"}:
        return PermissionDenied(MESSAGES["permissions"])
    if status == 401 or (token and reason in {"invalid_grant", "expired_token", "access_denied"}):
        return AuthorizationRequired(MESSAGES["authentication"])
    if status == 403:
        if write and reason not in {
            "insufficientPermissions",
            "insufficient_scope",
            "accessNotConfigured",
        }:
            return ReadOnlyResource(MESSAGES["read_only"])
        return PermissionDenied(MESSAGES["permissions"])
    if status == 404:
        return ResourceMissing(MESSAGES["missing_resource"])
    if status == 400 and write:
        return TrackUnavailable(MESSAGES["track_unavailable"])
    return ProviderError(MESSAGES["internal"])
