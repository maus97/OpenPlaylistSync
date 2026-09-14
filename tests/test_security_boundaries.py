import logging
import re
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ops import main as main_module
from ops.api import routes
from ops.config import Settings
from ops.configuration import load_app_settings, load_saved_settings, save_app_settings
from ops.db import Base, build_engine, get_db
from ops.main import create_app
from ops.models import (
    LocalAdministrator,
    ProviderAccount,
    ProviderSearchCache,
    SyncBaseline,
    SyncPair,
    SyncRun,
)
from ops.providers.types import ProviderTrack
from ops.security import middleware as authentication_middleware
from ops.security.bootstrap import bootstrap_token, consume_bootstrap_token, verify_bootstrap_token
from ops.security.crypto import CredentialCipher
from ops.security.logging import SensitiveQueryFilter, redact_query
from ops.security.network import client_address
from ops.storage.repositories import ProviderAccountRepository
from ops.sync.domain import ActionType, ReconciliationAction, ReconciliationPlan, Side, TrackState
from ops.sync.serialization import encode_plan


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _isolated_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    production: bool = False,
    ytmusic_configured: bool = False,
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
        ytmusic_client_id="ytmusic-client" if ytmusic_configured else None,
        ytmusic_client_secret="ytmusic-secret" if ytmusic_configured else None,
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(authentication_middleware, "SessionLocal", factory)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
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


def test_playlist_page_survives_expired_google_refresh(tmp_path, monkeypatch):
    from ops.providers.base import AuthorizationRequired

    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    with client:
        _complete_setup(client, settings)
        with factory() as session:
            account = ProviderAccount(
                provider_name="youtube_music", external_account_id="synthetic"
            )
            session.add(account)
            session.flush()
            ProviderAccountRepository(
                session, CredentialCipher(settings.credential_encryption_key)
            ).save_credentials(account, {"access_token": "synthetic-expired-token"})
            session.commit()

        def expired(*args):
            raise AuthorizationRequired("Google authorization expired; start the connection again.")

        monkeypatch.setattr(routes, "provider_for_account", expired)
        monkeypatch.setattr(routes, "load_app_settings", lambda _: settings)
        response = client.get("/pairs")
        assert response.status_code == 200
        assert "Authorization could not be renewed" in response.text
        assert "Connect YouTube Music" in response.text
        assert "Service status" in response.text


def test_automatic_settings_preserve_secrets_and_require_csrf(tmp_path, monkeypatch):
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    with client:
        _complete_setup(client, settings)
        with factory() as session:
            save_app_settings(session, {"spotify_client_secret": "synthetic-secret"}, settings)
            accounts = [
                ProviderAccount(provider_name=n, external_account_id=n)
                for n in ("spotify", "youtube_music")
            ]
            session.add_all(accounts)
            session.flush()
            pair = SyncPair(
                source_account_id=accounts[0].id,
                target_account_id=accounts[1].id,
                source_playlist_id="spotify:test",
                target_playlist_id="youtube_music:test",
            )
            session.add(pair)
            session.commit()
            pair_id = pair.id
        assert client.post("/settings/automation", data={"pair_ids": pair_id}).status_code == 403
        csrf = _csrf(client.get("/settings").text)
        response = client.post(
            "/settings/automation",
            data={"csrf_token": csrf, "pair_ids": pair_id},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with factory() as session:
            saved = load_saved_settings(session, settings)
            assert saved["spotify_client_secret"] == "synthetic-secret"
            assert len(saved["automatic_sync_bindings"]) == 1
        assert (
            client.post(
                "/settings/automation",
                data={"csrf_token": csrf, "pair_ids": 999},
                follow_redirects=False,
            ).status_code
            == 400
        )
        response = client.post(
            "/settings/automation", data={"csrf_token": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        with factory() as session:
            saved = load_saved_settings(session, settings)
            assert saved["automatic_sync_bindings"] == []
            assert saved["spotify_client_secret"] == "synthetic-secret"


def test_activity_shows_the_reviewed_change_details(tmp_path, monkeypatch):
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    with client:
        _complete_setup(client, settings)
        with factory() as session:
            spotify = ProviderAccount(provider_name="spotify", external_account_id="spotify")
            youtube = ProviderAccount(provider_name="youtube_music", external_account_id="youtube")
            session.add_all((spotify, youtube))
            session.flush()
            pair = SyncPair(
                source_account_id=spotify.id,
                target_account_id=youtube.id,
                source_playlist_id="spotify:test",
                target_playlist_id="youtube_music:test",
            )
            session.add(pair)
            session.flush()
            plan = ReconciliationPlan(
                actions=(
                    ReconciliationAction(
                        Side.TARGET,
                        ActionType.ADD_TRACK,
                        TrackState(
                            "text:afterglow|ed sheeran", "Afterglow", ("Ed Sheeran",), "spotify:one"
                        ),
                        "test",
                    ),
                ),
                conflicts=(),
            )
            session.add(SyncRun(pair_id=pair.id, status="applied", plan_json=encode_plan(plan)))
            session.commit()
        response = client.get("/runs")
        assert response.status_code == 200
        assert "Added to YouTube Music" in response.text
        assert "Afterglow" in response.text


def test_activity_hides_no_change_scheduled_previews(tmp_path, monkeypatch):
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    with client:
        _complete_setup(client, settings)
        with factory() as session:
            session.add(
                SyncRun(status="planned", plan_json=encode_plan(ReconciliationPlan((), ())))
            )
            session.add(SyncRun(status="review_failed"))
            session.commit()
        response = client.get("/runs")
        assert "<td>#1</td>" not in response.text
        assert "Review Failed" in response.text


def test_replacing_an_existing_baseline_is_csrf_protected(tmp_path, monkeypatch):
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    accepted_pair_ids: list[int] = []

    class Coordinator:
        def __init__(self, *_args) -> None:
            pass

        def accept_current_state(self, pair: SyncPair) -> None:
            accepted_pair_ids.append(pair.id)

    monkeypatch.setattr(routes, "SyncCoordinator", Coordinator)
    with client:
        _complete_setup(client, settings)
        with factory() as session:
            spotify = ProviderAccount(provider_name="spotify", external_account_id="spotify")
            youtube = ProviderAccount(provider_name="youtube_music", external_account_id="youtube")
            session.add_all((spotify, youtube))
            session.flush()
            pair = SyncPair(
                source_account_id=spotify.id,
                target_account_id=youtube.id,
                source_playlist_id="spotify:test",
                target_playlist_id="youtube_music:test",
            )
            session.add(pair)
            session.flush()
            session.add(
                SyncBaseline(
                    pair_id=pair.id,
                    account_id=spotify.id,
                    playlist_key="spotify:test",
                    source_provider="spotify",
                    target_provider="youtube_music",
                    snapshot_json="{}",
                    synchronized_at=datetime.now(UTC),
                )
            )
            session.commit()
            pair_id = pair.id
        csrf = _csrf(client.get("/pairs").text)
        accepted = client.post(
            f"/sync/baseline/{pair_id}",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted_pair_ids == [pair_id]


def test_candidate_picker_labels_clean_and_explicit_recordings() -> None:
    action = SimpleNamespace(track=SimpleNamespace(title="Peaches", artists=("Justin Bieber",)))
    choice = SimpleNamespace(
        action=action,
        action_index=0,
        candidates=(
            ProviderTrack(
                "spotify:explicit",
                "Peaches (feat. Daniel Caesar & Giveon)",
                ("Justin Bieber", "Daniel Caesar", "GIV\u0112ON"),
                album="Justice",
                explicit=True,
            ),
            ProviderTrack(
                "spotify:clean",
                "Peaches (feat. Daniel Caesar & Giveon)",
                ("Justin Bieber", "Daniel Caesar", "GIV\u0112ON"),
                album="Justice",
                explicit=False,
            ),
        ),
    )
    rendered = routes.templates.get_template("sync_plan.html").render(
        request=SimpleNamespace(session={"local_admin_authenticated": True}),
        csrf_token="test-csrf",
        error=None,
        plan=ReconciliationPlan((), ()),
        review=SimpleNamespace(baseline_upgrade_required=False, review_id=1),
        pair=SimpleNamespace(id=1),
        candidate_options=(choice,),
        unresolved_tracks=(),
        fingerprint="",
        approval_token="",
    )

    assert "Justice · Explicit" in rendered
    assert "Justice · Clean" in rendered


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
    assert (
        "form-action 'self' https://accounts.spotify.com"
        in response.headers["content-security-policy"]
    )
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


def test_https_mode_switch_is_persisted_and_applies_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    _complete_setup(client, settings)
    page = client.get("/settings")
    assert "Enable HTTPS mode" in page.text
    csrf = _csrf(page.text)

    response = client.post(
        "/settings",
        data={"csrf_token": csrf, "https_mode_enabled": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&restart_required=1"
    assert client.app.state.security_mode.https_enabled is False
    with factory() as session:
        assert load_saved_settings(session, settings)["session_cookie_secure"] is True
        restarted_settings = load_app_settings(session, settings)
    assert restarted_settings.https_mode_enabled is True

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    restarted_app = main_module.create_app()
    with TestClient(restarted_app, base_url="https://testserver") as restarted_client:
        assert restarted_client.get("/healthz").status_code == 200
        assert restarted_app.state.security_mode.https_enabled is True

    client.app.state.security_mode.https_enabled = True
    client.cookies.clear()
    secured = client.get("/auth/login")
    assert "secure" in secured.headers["set-cookie"].casefold()
    assert secured.headers["strict-transport-security"].startswith("max-age=31536000")


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

    calls = {"load": 0, "prepare": 0, "select": 0}

    class FakeCoordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def load_review(self, _pair, _review_id=None):  # type: ignore[no-untyped-def]
            calls["load"] += 1
            return None

        def prepare_review(self, _pair):  # type: ignore[no-untyped-def]
            calls["prepare"] += 1
            return SimpleNamespace(review_id=9, approval_token="one-time-token")

        def select_candidate(self, _pair, _review_id, _action_index, _candidate_id):  # type: ignore[no-untyped-def]
            calls["select"] += 1

    monkeypatch.setattr(routes, "SyncCoordinator", FakeCoordinator)
    displayed = client.get(f"/sync/plan/{pair_id}")
    assert displayed.status_code == 200
    assert calls == {"load": 1, "prepare": 0, "select": 0}

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
    assert (
        client.post(
            f"/sync/plan/{pair_id}/candidate",
            data={
                "review_id": "9",
                "action_index": "0",
                "candidate_id": "youtube_music:video",
            },
        ).status_code
        == 403
    )
    assert calls["select"] == 0
    selected = client.post(
        f"/sync/plan/{pair_id}/candidate",
        data={
            "csrf_token": csrf,
            "review_id": "9",
            "action_index": "0",
            "candidate_id": "youtube_music:video",
        },
        follow_redirects=False,
    )
    assert selected.status_code == 303
    assert selected.headers["location"] == f"/sync/plan/{pair_id}?review_id=9"
    assert calls["select"] == 1

    def conflicting_review(self, _pair):
        raise routes.TrackMappingConflict("Conflicting recording <script>unsafe</script>")

    monkeypatch.setattr(FakeCoordinator, "prepare_review", conflicting_review)
    rejected = client.post(f"/sync/plan/{pair_id}", data={"csrf_token": csrf})
    assert rejected.status_code == 409
    assert "text/html" in rejected.headers["content-type"]
    assert "Back to playlists" in rejected.text
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in rejected.text
    assert "<script>unsafe</script>" not in rejected.text
    assert "Apply changes" not in rejected.text


def test_ytmusicapi_reconnects_one_legacy_account_and_pauses_its_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeYouTubeMusicAuthService:
        def __init__(self, client_id: str, client_secret: str) -> None:
            assert (client_id, client_secret) == ("ytmusic-client", "ytmusic-secret")

        def request_code(self) -> dict[str, str]:
            return {
                "device_code": "test-device-code",
                "user_code": "ABC-DEF",
                "verification_url": "https://www.google.com/device",
            }

        def exchange_device_code(self, device_code: str) -> dict[str, object]:
            assert device_code == "test-device-code"
            return {
                "auth_scheme": "ytmusicapi_oauth",
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
                "expires_at": 4_102_444_800,
                "expires_in": 3600,
            }

    class FakeYTMusicApiProvider:
        def __init__(self, _token, *, client_id: str, client_secret: str) -> None:  # type: ignore[no-untyped-def]
            assert (client_id, client_secret) == ("ytmusic-client", "ytmusic-secret")

        def account_identity(self) -> tuple[str, str]:
            return ("ytmusicapi:handle:@listener", "Listener")

    monkeypatch.setattr(routes, "YouTubeMusicAuthService", FakeYouTubeMusicAuthService)
    monkeypatch.setattr(routes, "YTMusicApiProvider", FakeYTMusicApiProvider)
    client, factory, settings = _isolated_client(tmp_path, monkeypatch, ytmusic_configured=True)
    _complete_setup(client, settings)
    with factory() as session:
        youtube = ProviderAccount(
            provider_name="youtube_music", external_account_id="legacy-channel"
        )
        spotify = ProviderAccount(provider_name="spotify", external_account_id="listener")
        session.add_all((youtube, spotify))
        session.flush()
        ProviderAccountRepository(
            session, CredentialCipher(settings.credential_encryption_key or "")
        ).save_credentials(
            youtube, {"access_token": "legacy-token", "refresh_token": "legacy-refresh"}
        )
        pair = SyncPair(
            source_account_id=spotify.id,
            target_account_id=youtube.id,
            source_playlist_id="spotify:playlist",
            target_playlist_id="youtube_music:playlist",
        )
        session.add(pair)
        session.commit()
        youtube_id = youtube.id
        pair_id = pair.id

    csrf = _csrf(client.get("/settings").text)
    start = client.post("/auth/youtube_music/start", data={"csrf_token": csrf})
    assert start.status_code == 200
    response = client.post(
        "/auth/youtube_music/complete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/pairs?connected=youtube_music"
    with factory() as session:
        youtube = session.get(ProviderAccount, youtube_id)
        pair = session.get(SyncPair, pair_id)
        assert youtube is not None
        assert youtube.external_account_id == "ytmusicapi:handle:@listener"
        assert youtube.credentials_ciphertext is not None
        assert "new-access-token" not in youtube.credentials_ciphertext
        assert pair is not None and not pair.enabled


def test_disconnect_clears_account_scoped_search_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    _complete_setup(client, settings)
    with factory() as session:
        account = ProviderAccount(
            provider_name="youtube_music",
            external_account_id="ytmusicapi:handle:@listener",
            credentials_ciphertext="encrypted-test-value",
            credential_key_id="primary",
        )
        other = ProviderAccount(provider_name="spotify", external_account_id="listener")
        session.add_all((account, other))
        session.flush()
        pair = SyncPair(
            source_account_id=other.id,
            target_account_id=account.id,
            source_playlist_id="spotify:playlist",
            target_playlist_id="youtube_music:playlist",
        )
        session.add_all(
            (
                pair,
                ProviderSearchCache(
                    account_id=account.id,
                    provider_name="youtube_music",
                    track_fingerprint="a" * 64,
                    algorithm_version=1,
                    candidate_tracks_json="[]",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
        )
        session.commit()
        account_id = account.id
        pair_id = pair.id

    csrf = _csrf(client.get("/settings").text)
    response = client.post(
        "/accounts/youtube_music/disconnect",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with factory() as session:
        account = session.get(ProviderAccount, account_id)
        pair = session.get(SyncPair, pair_id)
        assert account is not None and account.credentials_ciphertext is None
        assert pair is not None and not pair.enabled
        assert session.scalar(select(ProviderSearchCache)) is None


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
