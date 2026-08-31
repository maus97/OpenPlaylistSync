from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.db import Base
from ops.security.local_auth import (
    ACCOUNT_FAILURE_LIMIT,
    PasswordPolicyError,
    create_administrator,
    is_locked,
    record_failure,
    record_success,
    source_is_limited,
    source_key,
    verify_password,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_password_is_hashed_and_verifiable(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")

    assert "a secure local passphrase" not in record.password_hash
    assert verify_password("a secure local passphrase", record.password_hash)
    assert not verify_password("not the password", record.password_hash)


def test_short_password_is_rejected(session: Session) -> None:
    with pytest.raises(PasswordPolicyError):
        create_administrator(session, "too-short")


def test_failed_attempts_lock_the_administrator(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")
    key = source_key("192.0.2.15")

    for _ in range(ACCOUNT_FAILURE_LIMIT):
        record_failure(session, record, key)

    assert is_locked(record)
    assert not is_locked(record, datetime.now(UTC) + timedelta(minutes=16))


def test_source_rate_limit_and_success_reset(session: Session) -> None:
    record = create_administrator(session, "a secure local passphrase")
    key = source_key("192.0.2.15")

    for _ in range(10):
        record_failure(session, record, key)
    assert source_is_limited(session, key)

    record_success(session, record)
    assert not is_locked(record)
