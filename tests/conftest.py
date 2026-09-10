"""Shared test fixtures."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ops.db import Base, get_db
from ops.main import app


@pytest.fixture(autouse=True)
def override_db():
    """Replace the real database with an in-memory SQLite DB for every test."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def _get_test_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()
