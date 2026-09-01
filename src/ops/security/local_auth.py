"""Local administrator authentication and concurrency-safe sign-in throttling."""

import base64
import hashlib
import hmac
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ops.models import LocalAdministrator, LoginRateLimit

PASSWORD_MINIMUM_LENGTH = 12
PASSWORD_MAXIMUM_LENGTH = 256
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 3
SCRYPT_MAX_MEMORY = 128 * 1024 * 1024
SOURCE_FAILURE_LIMIT = 5
SOURCE_WINDOW = timedelta(minutes=15)
PASSWORD_VERIFICATION_CONCURRENCY = 2

_password_slots = BoundedSemaphore(PASSWORD_VERIFICATION_CONCURRENCY)


class PasswordPolicyError(ValueError):
    """Raised when a submitted local administrator password is not acceptable."""


class PasswordVerificationBusy(RuntimeError):
    """Raised instead of queuing unbounded memory-hard password operations."""


@dataclass(frozen=True, slots=True)
class LoginAttemptReservation:
    """The result of atomically reserving one source-scoped sign-in attempt."""

    allowed: bool
    retry_after_seconds: int


def validate_password(password: str) -> None:
    if not PASSWORD_MINIMUM_LENGTH <= len(password) <= PASSWORD_MAXIMUM_LENGTH:
        raise PasswordPolicyError(
            "Use a password between "
            f"{PASSWORD_MINIMUM_LENGTH} and {PASSWORD_MAXIMUM_LENGTH} characters."
        )
    if password.strip() != password:
        raise PasswordPolicyError("Do not start or end the password with whitespace.")


def hash_password(password: str) -> str:
    """Create a versioned scrypt verifier using a unique random salt."""

    validate_password(password)
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=SCRYPT_MAX_MEMORY,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a verifier without leaking a timing signal for the comparison."""

    if not isinstance(password, str) or len(password) > PASSWORD_MAXIMUM_LENGTH:
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected_bytes = base64.urlsafe_b64decode(expected)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_bytes),
            maxmem=SCRYPT_MAX_MEMORY,
        )
        return hmac.compare_digest(derived, expected_bytes)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


@contextmanager
def password_verification_slot():
    """Permit only a small bounded number of concurrent memory-hard checks."""

    if not _password_slots.acquire(blocking=False):
        raise PasswordVerificationBusy("sign-in verification is busy")
    try:
        yield
    finally:
        _password_slots.release()


def password_needs_rehash(encoded: str) -> bool:
    try:
        algorithm, n, r, p, _salt, _expected = encoded.split("$")
        return (
            algorithm != "scrypt" or int(n) != SCRYPT_N or int(r) != SCRYPT_R or int(p) != SCRYPT_P
        )
    except (TypeError, ValueError):
        return True


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def source_key(client_host: str | None) -> str:
    """Keep the rate limiter useful without retaining the raw client address."""

    return hashlib.sha256((client_host or "unknown").encode("utf-8")).hexdigest()


def administrator(session: Session) -> LocalAdministrator | None:
    return session.get(LocalAdministrator, 1)


def create_administrator(session: Session, password: str) -> LocalAdministrator:
    """Atomically create the sole administrator, allowing only one first-run winner."""

    if administrator(session) is not None:
        raise ValueError("local administrator is already configured")
    record = LocalAdministrator(
        id=1,
        password_hash=hash_password(password),
        password_changed_at=datetime.now(UTC),
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("local administrator is already configured") from exc
    return record


def reserve_login_attempt(
    session: Session,
    key: str,
    *,
    now: datetime | None = None,
) -> LoginAttemptReservation:
    """Atomically count an attempt before running scrypt and enforce a per-source window."""

    now = now or datetime.now(UTC)
    cutoff = now - SOURCE_WINDOW
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        expired = LoginRateLimit.window_started_at <= cutoff
        statement = sqlite_insert(LoginRateLimit).values(
            source_key=key,
            window_started_at=now,
            failed_attempts=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[LoginRateLimit.source_key],
            set_={
                "window_started_at": case((expired, now), else_=LoginRateLimit.window_started_at),
                "failed_attempts": case((expired, 1), else_=LoginRateLimit.failed_attempts + 1),
            },
        )
        reserved = session.execute(
            statement.returning(
                LoginRateLimit.failed_attempts,
                LoginRateLimit.window_started_at,
            )
        ).one()
        reserved_failures = int(reserved.failed_attempts)
        reserved_window_started_at = _utc(reserved.window_started_at)
    else:
        rate = session.scalar(
            select(LoginRateLimit).where(LoginRateLimit.source_key == key).with_for_update()
        )
        if rate is None or _utc(rate.window_started_at) <= cutoff:
            rate = LoginRateLimit(source_key=key, window_started_at=now, failed_attempts=1)
        else:
            rate.failed_attempts += 1
        session.add(rate)
        session.flush()
        reserved_failures = rate.failed_attempts
        reserved_window_started_at = _utc(rate.window_started_at)
    session.commit()
    expires_at = reserved_window_started_at + SOURCE_WINDOW
    retry_after = max(1, int((expires_at - now).total_seconds()))
    return LoginAttemptReservation(
        allowed=reserved_failures <= SOURCE_FAILURE_LIMIT,
        retry_after_seconds=retry_after,
    )


def record_success(
    session: Session,
    record: LocalAdministrator,
    key: str,
    *,
    password: str | None = None,
) -> None:
    """Clear only this source's failures and opportunistically strengthen legacy hashes."""

    session.execute(delete(LoginRateLimit).where(LoginRateLimit.source_key == key))
    record.failed_attempts = 0
    record.locked_until = None
    if password is not None and password_needs_rehash(record.password_hash):
        record.password_hash = hash_password(password)
        record.password_changed_at = datetime.now(UTC)
    session.add(record)
    session.commit()


def change_password(session: Session, record: LocalAdministrator, new_password: str) -> None:
    record.password_hash = hash_password(new_password)
    record.session_generation += 1
    record.password_changed_at = datetime.now(UTC)
    record.failed_attempts = 0
    record.locked_until = None
    session.execute(delete(LoginRateLimit))
    session.add(record)
    session.commit()
