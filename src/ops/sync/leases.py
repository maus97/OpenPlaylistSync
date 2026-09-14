"""Database-backed exclusion for review, baseline, and Apply operations."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.orm import Session

from ops.models import SyncPair

PAIR_LEASE_DURATION = timedelta(minutes=30)


class PairOperationBusy(RuntimeError):
    """Raised when another worker already owns the pair operation boundary."""


class PairLeaseLost(RuntimeError):
    """Raised when an operation no longer owns its persisted lease."""


@dataclass(frozen=True, slots=True)
class PairLease:
    bind: Engine
    pair_id: int
    token: str
    kind: str = "operation"

    def assert_owned(self, session) -> None:
        owned = session.scalar(
            select(SyncPair.id).where(
                SyncPair.id == self.pair_id,
                getattr(SyncPair, f"{self.kind}_lock_token") == self.token,
                getattr(SyncPair, f"{self.kind}_lock_expires_at") > datetime.now(UTC),
            )
        )
        if owned is None:
            raise PairLeaseLost("the synchronization lease expired; retry safely")

    def renew(self, duration: timedelta = PAIR_LEASE_DURATION) -> None:
        token_column = getattr(SyncPair, f"{self.kind}_lock_token")
        expires_column = getattr(SyncPair, f"{self.kind}_lock_expires_at")
        with Session(self.bind) as session:
            result = session.execute(
                update(SyncPair)
                .where(
                    SyncPair.id == self.pair_id,
                    token_column == self.token,
                    expires_column > datetime.now(UTC),
                )
                .values(**{f"{self.kind}_lock_expires_at": datetime.now(UTC) + duration})
            )
            session.commit()
        if result.rowcount != 1:
            raise PairLeaseLost("the synchronization lease was lost; stop and review again")

    def release(self) -> None:
        token_column = getattr(SyncPair, f"{self.kind}_lock_token")
        with Session(self.bind) as session:
            session.execute(
                update(SyncPair)
                .where(
                    SyncPair.id == self.pair_id,
                    token_column == self.token,
                )
                .values(**{f"{self.kind}_lock_token": None, f"{self.kind}_lock_expires_at": None})
            )
            session.commit()


def acquire_pair_lease(
    session: Session,
    pair_id: int,
    duration: timedelta = PAIR_LEASE_DURATION,
    *,
    kind: str = "operation",
) -> PairLease:
    """Atomically acquire a cross-process lease without holding a DB transaction open."""

    bind = session.get_bind()
    if kind not in {"operation", "automatic"}:
        raise ValueError("unsupported lease kind")
    token_column = getattr(SyncPair, f"{kind}_lock_token")
    expires_column = getattr(SyncPair, f"{kind}_lock_expires_at")
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with Session(bind) as lease_session:
        result = lease_session.execute(
            update(SyncPair)
            .where(
                SyncPair.id == pair_id,
                or_(
                    token_column.is_(None),
                    expires_column.is_(None),
                    expires_column <= now,
                ),
            )
            .values(**{f"{kind}_lock_token": token, f"{kind}_lock_expires_at": now + duration})
        )
        lease_session.commit()
    if result.rowcount != 1:
        raise PairOperationBusy("another review or synchronization is already running")
    session.expire_all()
    return PairLease(bind=bind, pair_id=pair_id, token=token, kind=kind)
