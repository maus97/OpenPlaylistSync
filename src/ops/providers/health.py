"""Current provider health, safe incidents and durable scheduler retry decisions."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from ops.models import ProviderAccount, ProviderIncident
from ops.providers.errors import MESSAGES, diagnostic


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def verified(
    session, account_id, started_at, *, operation="reconnect", resource=None, authenticated=True
):
    """Only resolve failures older than the successful operation's start.

    A list read proves authentication but cannot prove a playlist writable. Public
    catalogue searches must pass authenticated=False. Caller commits its transaction.
    """
    field = "auth_verified_at" if operation == "refresh" else "verified_at"
    column = getattr(ProviderAccount, field)
    if authenticated:
        session.execute(
            update(ProviderAccount)
            .execution_options(synchronize_session="fetch")
            .where(ProviderAccount.id == account_id, or_(column.is_(None), column < started_at))
            .values(**{field: started_at})
        )
    scope = (ProviderIncident.category == "authentication") if authenticated else False
    scope = or_(
        scope,
        (ProviderIncident.operation == operation)
        & (ProviderIncident.resource == resource)
        & (True if authenticated else ProviderIncident.category != "authentication"),
    )
    session.execute(
        update(ProviderIncident)
        .execution_options(synchronize_session="fetch")
        .where(
            ProviderIncident.account_id == account_id,
            ProviderIncident.resolved_at.is_(None),
            ProviderIncident.occurred_at <= started_at,
            scope,
        )
        .values(resolved_at=datetime.now(UTC))
    )


def record_failure(session, exc, pair_id=None):
    """Persist after caller rollback; never serialize the raw exception or response."""
    info = diagnostic(exc)
    account_id = info["account_id"]
    if account_id is None:
        return info
    kind = info["category"]
    started = getattr(exc, "operation_started_at", datetime.now(UTC))
    prior = list(
        session.scalars(
            select(ProviderIncident).where(
                ProviderIncident.account_id == account_id,
                ProviderIncident.pair_id == pair_id,
                ProviderIncident.category == kind,
                ProviderIncident.resolved_at.is_(None),
            )
        )
    )
    seconds = {
        "authentication": 3600,
        "permissions": 21600,
        "read_only": 21600,
        "missing_resource": 21600,
        "rate_limit": 3600,
    }.get(kind, min(3600, 60 * (2 ** min(len(prior), 6))))
    if kind == "rate_limit" and getattr(exc, "retry_after_seconds", None):
        seconds = max(1, exc.retry_after_seconds)
    account = session.get(ProviderAccount, account_id, populate_existing=True)
    recovered = (
        kind == "authentication"
        and account
        and any(
            value and utc(value) > utc(started)
            for value in (account.verified_at, account.auth_verified_at)
        )
    )
    incident = ProviderIncident(
        account_id=account_id,
        pair_id=pair_id,
        category=kind,
        operation=info["operation"] or "unknown",
        resource=getattr(exc, "resource", None),
        occurred_at=started,
        retry_at=datetime.now(UTC) + timedelta(seconds=seconds),
        resolved_at=datetime.now(UTC) if recovered else None,
    )
    session.add(incident)
    session.flush()
    info["incident_id"] = incident.id
    return info


def active_incidents(session, pair):
    return list(
        session.scalars(
            select(ProviderIncident)
            .where(
                ProviderIncident.account_id.in_((pair.source_account_id, pair.target_account_id)),
                or_(ProviderIncident.pair_id.is_(None), ProviderIncident.pair_id == pair.id),
                ProviderIncident.resolved_at.is_(None),
            )
            .order_by(ProviderIncident.occurred_at.desc())
        )
    )


def legacy_unresolved(session, pair, run):
    if run is None:
        return False
    try:
        info = json.loads(run.summary_json or "{}")
    except (ValueError, TypeError):
        info = {}
    if info.get("incident_id"):
        return False
    if run.status != "authorization_required" and info.get("error_type") != "AuthorizationRequired":
        return False
    accounts = [
        session.get(ProviderAccount, aid, populate_existing=True)
        for aid in (pair.source_account_id, pair.target_account_id)
    ]
    return not all(
        a and a.verified_at and utc(a.verified_at) > utc(run.completed_at or run.started_at)
        for a in accounts
    )


def retry_pending(session, pair, run):
    now = datetime.now(UTC)
    if any(utc(i.retry_at) > now for i in active_incidents(session, pair)):
        return True
    return legacy_unresolved(session, pair, run) and (
        now < utc(run.completed_at or run.started_at) + timedelta(hours=1)
    )


def pair_health(session, pair, run):
    incidents = active_incidents(session, pair)
    if incidents:
        item = next((i for i in incidents if i.category == "authentication"), incidents[0])
        account = session.get(ProviderAccount, item.account_id)
        label = {"spotify": "Spotify", "youtube_music": "YouTube Music"}[account.provider_name]
        title = {
            "authentication": "Connection needs attention",
            "rate_limit": "Waiting for provider",
            "permissions": "Access needs attention",
            "read_only": "Playlist is read-only",
            "missing_resource": "Playlist unavailable",
        }.get(item.category, "Waiting to retry")
        return (
            title,
            f"{label}: {MESSAGES[item.category]} Next eligible retry: {item.retry_at} UTC.",
        )
    if legacy_unresolved(session, pair, run):
        return (
            "Connection not yet verified",
            "An earlier run lost access. Verify both services to resolve it.",
        )
    return None


class ObservedProvider:
    """Attach provider/operation context without replaying composite playlist writes."""

    def __init__(self, provider, session, account_id):
        self._provider = provider
        self._session = session
        self._account_id = account_id
        self.name = provider.name

    def __getattr__(self, name):
        value = getattr(self._provider, name)
        if not callable(value):
            return value

        def operation(*args, **kwargs):
            started = datetime.now(UTC)
            candidate_resource = args[0] if args else kwargs.get("playlist_id")
            resource = (
                candidate_resource
                if (
                    isinstance(candidate_resource, str)
                    and name in {"get_playlist", "add_tracks", "remove_tracks"}
                )
                else None
            )
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                exc.provider = self.name
                exc.account_id = self._account_id
                exc.operation = getattr(exc, "operation", name)
                # A renewal belongs to the account, even when a playlist HTTP
                # request prompted it. Otherwise renewal success cannot clear
                # its own temporary failure on a resource-scoped incident.
                exc.resource = None if exc.operation == "refresh" else resource
                exc.operation_started_at = getattr(exc, "operation_started_at", started)
                raise
            if name in {
                "list_playlists",
                "get_playlist",
                "create_playlist",
                "add_tracks",
                "remove_tracks",
            }:
                verified(
                    self._session, self._account_id, started, operation=name, resource=resource
                )
            elif name in {"search_track", "search_candidates"}:
                verified(
                    self._session,
                    self._account_id,
                    started,
                    operation=name,
                    resource=resource,
                    authenticated=False,
                )
            return result

        return operation
