"""SQLAlchemy engine and session wiring."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ops.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for all persisted OPS models."""


def build_engine(settings: Settings) -> Engine:
    """Build a database engine from configuration."""

    connect_args = (
        {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    )
    return create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)


engine = build_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped database session."""

    with SessionLocal() as session:
        yield session
