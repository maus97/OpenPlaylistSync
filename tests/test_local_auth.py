from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base, build_engine
from ops.models import LoginRateLimit
from ops.security.local_auth import (
    SOURCE_FAILURE_LIMIT,
    PasswordPolicyError,
    create_administrator,
    password_needs_rehash,
    record_success,
    reserve_login_attempt,
    source_key,
    verify_password,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_password_is_hashed_and_verifiable_at_current_work_factor(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")

    assert "a secure local passphrase" not in record.password_hash
    assert record.password_hash.startswith("scrypt$32768$8$3$")
    assert verify_password("a secure local passphrase", record.password_hash)
    assert not verify_password("not the password", record.password_hash)
    assert not password_needs_rehash(record.password_hash)


def test_short_password_is_rejected(session: Session) -> None:
    with pytest.raises(PasswordPolicyError):
        create_administrator(session, "too-short")


def test_rate_limit_is_source_scoped_and_does_not_lock_the_account(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")
    first_key = source_key("192.0.2.15")
    second_key = source_key("192.0.2.16")

    for _ in range(SOURCE_FAILURE_LIMIT):
        assert reserve_login_attempt(session, first_key).allowed

    blocked = reserve_login_attempt(session, first_key)
    assert not blocked.allowed
    assert blocked.retry_after_seconds > 0
    assert reserve_login_attempt(session, second_key).allowed
    assert record.locked_until is None


def test_expired_window_and_success_reset_only_the_relevant_source(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")
    key = source_key("192.0.2.15")
    other_key = source_key("192.0.2.16")
    now = datetime.now(UTC)

    reserve_login_attempt(session, key, now=now - timedelta(minutes=16))
    assert reserve_login_attempt(session, key, now=now).allowed
    reserve_login_attempt(session, other_key, now=now)
    record_success(session, record, key)

    assert session.get(LoginRateLimit, key) is None
    assert session.get(LoginRateLimit, other_key) is not None


def test_concurrent_attempt_reservations_are_atomic(tmp_path: Path) -> None:
    engine = build_engine(
        Settings(database_url=f"sqlite:///{(tmp_path / 'rate-limit.db').as_posix()}")
    )
    Base.metadata.create_all(engine)
    key = source_key("192.0.2.20")

    def reserve() -> bool:
        with Session(engine) as worker_session:
            return reserve_login_attempt(worker_session, key).allowed

    with ThreadPoolExecutor(max_workers=10) as workers:
        allowed = list(workers.map(lambda _index: reserve(), range(10)))

    assert sum(allowed) == SOURCE_FAILURE_LIMIT
    with Session(engine) as check:
        assert check.get(LoginRateLimit, key).failed_attempts == 10
    engine.dispose()
