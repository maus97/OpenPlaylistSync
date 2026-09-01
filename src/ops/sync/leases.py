"""Database-backed exclusion for review, baseline, and Apply operations."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, or_, update
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

    def renew(self, duration: timedelta = PAIR_LEASE_DURATION) -> None:
        with Session(self.bind) as session:
            result = session.execute(
                update(SyncPair)
                .where(
                    SyncPair.id == self.pair_id,
                    SyncPair.operation_lock_token == self.token,
                )
                .values(operation_lock_expires_at=datetime.now(UTC) + duration)
            )
            session.commit()
        if result.rowcount != 1:
            raise PairLeaseLost("the synchronization lease was lost; stop and review again")

    def release(self) -> None:
        with Session(self.bind) as session:
            session.execute(
                update(SyncPair)
                .where(
                    SyncPair.id == self.pair_id,
                    SyncPair.operation_lock_token == self.token,
                )
                .values(operation_lock_token=None, operation_lock_expires_at=None)
            )
            session.commit()


def acquire_pair_lease(
    session: Session,
    pair_id: int,
    duration: timedelta = PAIR_LEASE_DURATION,
) -> PairLease:
    """Atomically acquire a cross-process lease without holding a DB transaction open."""

    bind = session.get_bind()
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with Session(bind) as lease_session:
        result = lease_session.execute(
            update(SyncPair)
            .where(
                SyncPair.id == pair_id,
                or_(
                    SyncPair.operation_lock_token.is_(None),
                    SyncPair.operation_lock_expires_at.is_(None),
                    SyncPair.operation_lock_expires_at <= now,
                ),
            )
            .values(
                operation_lock_token=token,
                operation_lock_expires_at=now + duration,
            )
        )
        lease_session.commit()
    if result.rowcount != 1:
        raise PairOperationBusy("another review or synchronization is already running")
    session.expire_all()
    return PairLease(bind=bind, pair_id=pair_id, token=token)
