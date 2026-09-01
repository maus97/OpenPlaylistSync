"""SQLAlchemy engine and session wiring."""

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ops.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for all persisted OPS models."""


def build_engine(settings: Settings) -> Engine:
    """Build a database engine from configuration."""

    sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False, "timeout": 30} if sqlite else {}
    engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    if sqlite:
        database = make_url(settings.database_url).database

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()
            if database and database != ":memory:":
                path = Path(database)
                try:
                    path.chmod(0o600)
                except OSError:
                    pass

    return engine


engine = build_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped database session."""

    with SessionLocal() as session:
        yield session
