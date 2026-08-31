"""Local administrator authentication and persistent brute-force protections."""

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ops.models import LocalAdministrator, LoginRateLimit

PASSWORD_MINIMUM_LENGTH = 12
PASSWORD_MAXIMUM_LENGTH = 256
ACCOUNT_FAILURE_LIMIT = 5
ACCOUNT_LOCK_DURATION = timedelta(minutes=15)
SOURCE_FAILURE_LIMIT = 10
SOURCE_WINDOW = timedelta(minutes=15)


class PasswordPolicyError(ValueError):
    """Raised when a submitted local administrator password is not acceptable."""


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
        password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024
    )
    return "scrypt$32768$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a verifier without leaking a timing signal for the comparison."""

    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.urlsafe_b64decode(expected)),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(derived, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def source_key(client_host: str | None) -> str:
    """Keep the rate limiter useful without retaining the raw LAN address."""

    return hashlib.sha256((client_host or "unknown").encode("utf-8")).hexdigest()


def administrator(session: Session) -> LocalAdministrator | None:
    return session.get(LocalAdministrator, 1)


def create_administrator(session: Session, password: str) -> LocalAdministrator:
    if administrator(session) is not None:
        raise ValueError("local administrator is already configured")
    record = LocalAdministrator(
        id=1,
        password_hash=hash_password(password),
        password_changed_at=datetime.now(UTC),
    )
    session.add(record)
    session.commit()
    return record


def is_locked(record: LocalAdministrator, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return record.locked_until is not None and _utc(record.locked_until) > now


def source_is_limited(session: Session, key: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    record = session.get(LoginRateLimit, key)
    return bool(
        record
        and _utc(record.window_started_at) + SOURCE_WINDOW > now
        and record.failed_attempts >= SOURCE_FAILURE_LIMIT
    )


def record_failure(session: Session, record: LocalAdministrator, key: str) -> None:
    now = datetime.now(UTC)
    rate = session.get(LoginRateLimit, key)
    if rate is None or _utc(rate.window_started_at) + SOURCE_WINDOW <= now:
        rate = LoginRateLimit(source_key=key, window_started_at=now, failed_attempts=1)
    else:
        rate.failed_attempts += 1
    session.add(rate)
    record.failed_attempts += 1
    if record.failed_attempts >= ACCOUNT_FAILURE_LIMIT:
        record.failed_attempts = 0
        record.locked_until = now + ACCOUNT_LOCK_DURATION
    session.add(record)
    session.commit()


def record_success(session: Session, record: LocalAdministrator) -> None:
    record.failed_attempts = 0
    record.locked_until = None
    session.add(record)
    session.commit()


def change_password(session: Session, record: LocalAdministrator, new_password: str) -> None:
    record.password_hash = hash_password(new_password)
    record.session_generation += 1
    record.password_changed_at = datetime.now(UTC)
    record.failed_attempts = 0
    record.locked_until = None
    session.add(record)
    session.commit()
