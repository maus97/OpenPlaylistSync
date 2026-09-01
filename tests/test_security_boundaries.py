import logging
import re
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ops.api import routes
from ops.config import Settings
from ops.db import Base, build_engine, get_db
from ops.main import create_app
from ops.models import LocalAdministrator, ProviderAccount, SyncPair
from ops.security import middleware as authentication_middleware
from ops.security.bootstrap import bootstrap_token, consume_bootstrap_token, verify_bootstrap_token
from ops.security.logging import SensitiveQueryFilter, redact_query
from ops.security.network import client_address


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _isolated_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    production: bool = False,
) -> tuple[TestClient, sessionmaker[Session], Settings]:
    database_path = tmp_path / "ops.db"
    settings = Settings(
        environment="production" if production else "test",
        database_url=f"sqlite:///{database_path.as_posix()}",
        data_dir=str(tmp_path / "data"),
        secret_dir=str(tmp_path / "secrets"),
        session_secret="s" * 64,
        credential_encryption_key=Fernet.generate_key().decode("ascii"),
        bootstrap_token="b" * 48,
        allowed_hosts="testserver",
        scheduler_enabled=False,
        max_request_body_bytes=1024,
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(authentication_middleware, "SessionLocal", factory)
    app = create_app(settings)

    def isolated_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[routes.settings] = lambda: settings
    scheme = "https" if production else "http"
    return TestClient(app, base_url=f"{scheme}://testserver"), factory, settings


def _complete_setup(client: TestClient, settings: Settings) -> str:
    setup = client.get("/auth/setup")
    csrf = _csrf(setup.text)
    response = client.post(
        "/auth/setup",
        data={
            "csrf_token": csrf,
            "bootstrap_code": settings.bootstrap_token,
            "password": "Correct horse battery staple!",
            "password_confirmation": "Correct horse battery staple!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def test_privileged_routes_require_authentication_and_setup_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)

    for path in ("/", "/settings", "/pairs", "/runs", "/docs", "/openapi.json"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/setup"

    setup = client.get("/auth/setup")
    csrf = _csrf(setup.text)
    missing_csrf = client.post(
        "/auth/setup",
        data={
            "bootstrap_code": settings.bootstrap_token,
            "password": "Correct horse battery staple!",
            "password_confirmation": "Correct horse battery staple!",
        },
    )
    assert missing_csrf.status_code == 403

    rejected = client.post(
        "/auth/setup",
        data={
            "csrf_token": csrf,
            "bootstrap_code": "wrong-setup-code",
            "password": "Correct horse battery staple!",
            "password_confirmation": "Correct horse battery staple!",
        },
    )
    assert rejected.status_code == 403
    with factory() as session:
        assert session.get(LocalAdministrator, 1) is None

    _complete_setup(client, settings)
    assert client.get("/settings").status_code == 200
    assert client.get("/auth/setup", follow_redirects=False).headers["location"] == "/auth/login"


def test_security_headers_trusted_host_secure_cookie_and_body_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _factory, _settings = _isolated_client(tmp_path, monkeypatch, production=True)
    response = client.get("/auth/setup")

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].casefold()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")

    bad_host = client.get("https://attacker.invalid/auth/setup")
    assert bad_host.status_code == 400

    oversized = client.post(
        "/auth/login",
        content=b"x" * 2048,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert oversized.status_code == 413


def test_review_and_oauth_initiation_are_csrf_protected_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    _complete_setup(client, settings)
    with factory() as session:
        source = ProviderAccount(provider_name="spotify", external_account_id="source")
        target = ProviderAccount(provider_name="youtube_music", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source",
            target_playlist_id="youtube_music:target",
        )
        session.add(pair)
        session.commit()
        pair_id = pair.id

    calls = {"load": 0, "prepare": 0}

    class FakeCoordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def load_review(self, _pair, _review_id=None):  # type: ignore[no-untyped-def]
            calls["load"] += 1
            return None

        def prepare_review(self, _pair):  # type: ignore[no-untyped-def]
            calls["prepare"] += 1
            return SimpleNamespace(review_id=9, approval_token="one-time-token")

    monkeypatch.setattr(routes, "SyncCoordinator", FakeCoordinator)
    displayed = client.get(f"/sync/plan/{pair_id}")
    assert displayed.status_code == 200
    assert calls == {"load": 1, "prepare": 0}

    for path in ("/auth/spotify/start", "/auth/youtube_music/start"):
        assert client.get(path).status_code == 405

    assert client.post(f"/sync/plan/{pair_id}").status_code == 403
    assert calls["prepare"] == 0
    csrf = _csrf(client.get("/settings").text)
    created = client.post(
        f"/sync/plan/{pair_id}",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == f"/sync/plan/{pair_id}?review_id=9"
    assert calls["prepare"] == 1


def test_generated_bootstrap_token_is_private_one_time_state(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        secret_dir=str(tmp_path / "secrets"),
    )
    token = bootstrap_token(settings)
    token_path = tmp_path / "secrets" / ".ops-bootstrap-token"

    assert len(token) >= 32
    assert token_path.exists()
    if token_path.stat().st_mode:
        assert token_path.stat().st_mode & 0o077 == 0
    verify_bootstrap_token(settings, token)
    consume_bootstrap_token(settings)
    assert not token_path.exists()
    assert bootstrap_token(settings) != token


def test_sqlite_foreign_keys_and_private_file_mode(tmp_path: Path) -> None:
    database_path = tmp_path / "private.db"
    engine = build_engine(Settings(database_url=f"sqlite:///{database_path.as_posix()}"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SyncPair(
                source_account_id=999,
                target_account_id=1000,
                source_playlist_id="source",
                target_playlist_id="target",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    if database_path.stat().st_mode:
        assert database_path.stat().st_mode & 0o077 == 0
    engine.dispose()


def test_oauth_query_logging_redacts_values() -> None:
    original = "/auth/spotify/callback?code=secret-code&state=secret-state&safe=value"
    redacted = redact_query(original)
    assert "secret-code" not in redacted
    assert "secret-state" not in redacted
    assert "safe=value" in redacted

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", original, "1.1", 200),
        None,
    )
    assert SensitiveQueryFilter().filter(record)
    assert "secret-code" not in str(record.args)


def test_forwarded_client_address_requires_a_trusted_peer() -> None:
    from starlette.requests import Request

    def request(peer: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [(b"x-forwarded-for", b"198.51.100.8, 10.0.0.5")],
                "client": (peer, 1234),
                "server": ("testserver", 80),
            }
        )

    settings = Settings(trusted_proxy_ips="10.0.0.0/8")
    assert client_address(request("203.0.113.9"), settings) == "203.0.113.9"
    assert client_address(request("10.0.0.5"), settings) == "198.51.100.8"


def test_create_app_rejects_missing_session_secret() -> None:
    with pytest.raises(RuntimeError, match="session secret"):
        create_app(Settings(session_secret=None))
