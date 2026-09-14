"""Recovery and retry behavior with synthetic providers, never real playlists."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import requests
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ops.auth.credentials import credentials_for
from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService
from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, ProviderIncident, SyncPair, SyncRun
from ops.providers.base import AuthorizationRequired, ProviderUnavailable, RateLimited
from ops.providers.errors import NetworkFailure, category, http_failure
from ops.providers.health import (
    ObservedProvider,
    active_incidents,
    legacy_unresolved,
    pair_health,
    record_failure,
    retry_pending,
    utc,
    verified,
)
from ops.providers.spotify import SpotifyProvider
from ops.providers.types import ProviderPlaylist
from ops.providers.youtube_music import _YouTubeDataApiClient
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import ProviderAccountRepository, SyncRunRepository
from ops.sync.automatic import automation_binding, run_automatic_pair
from ops.sync.coordinator import SyncCoordinator
from ops.sync.leases import PairOperationBusy, acquire_pair_lease


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        accounts = [
            ProviderAccount(provider_name=n, external_account_id=n)
            for n in ("spotify", "youtube_music")
        ]
        session.add_all(accounts)
        session.flush()
        pair = SyncPair(
            source_account_id=accounts[0].id,
            target_account_id=accounts[1].id,
            source_playlist_id="spotify:source",
            target_playlist_id="youtube_music:target",
        )
        session.add(pair)
        session.commit()
        yield session, accounts, pair
    engine.dispose()


def failure(session, account, pair, exc=None, operation="get_playlist"):
    exc = exc or AuthorizationRequired("PRIVATE RESPONSE MUST NOT BE STORED")
    exc.account_id = account.id
    exc.provider = account.provider_name
    exc.operation = operation
    exc.resource = "synthetic-playlist"
    info = record_failure(session, exc, pair.id)
    run = SyncRun(
        pair_id=pair.id,
        status="authorization_required",
        summary_json=json.dumps(info),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return run


@pytest.mark.parametrize("index", [0, 1])
def test_recovery_is_account_specific_persistent_and_preserves_history(context, index):
    s, accounts, pair = context
    run = failure(s, accounts[index], pair)
    info = json.loads(run.summary_json)
    assert info["provider"] == accounts[index].provider_name
    assert info["category"] == "authentication"
    assert "PRIVATE" not in run.summary_json
    assert retry_pending(s, pair, run)
    verified(s, accounts[1 - index].id, datetime.now(UTC))
    s.commit()
    assert retry_pending(s, pair, run)
    assert pair_health(s, pair, run)[0] == "Connection needs attention"
    verified(s, accounts[index].id, datetime.now(UTC))
    s.commit()
    with Session(s.get_bind()) as restarted:
        current_pair = restarted.get(SyncPair, pair.id)
        current_run = restarted.get(SyncRun, run.id)
        assert not retry_pending(restarted, current_pair, current_run)
        assert pair_health(restarted, current_pair, current_run) is None
        assert current_run.status == "authorization_required"
        assert restarted.get(ProviderIncident, info["incident_id"]).resolved_at


def test_legacy_auth_requires_both_verified_after_failure(context):
    s, accounts, pair = context
    run = SyncRun(pair_id=pair.id, status="authorization_required", started_at=datetime.now(UTC))
    s.add(run)
    s.commit()
    verified(s, accounts[0].id, datetime.now(UTC))
    assert legacy_unresolved(s, pair, run)
    verified(s, accounts[1].id, datetime.now(UTC))
    s.commit()
    assert not legacy_unresolved(s, pair, run)
    assert not retry_pending(s, pair, run)


def test_old_success_cannot_clear_new_failure_and_old_failure_cannot_undo_recovery(context):
    s, accounts, pair = context
    before = datetime.now(UTC) - timedelta(minutes=1)
    run = failure(s, accounts[0], pair)
    verified(s, accounts[0].id, before)
    assert retry_pending(s, pair, run)
    verified(s, accounts[0].id, datetime.now(UTC))
    s.commit()
    exc = AuthorizationRequired("old request returned late")
    exc.account_id = accounts[0].id
    exc.operation_started_at = before
    info = record_failure(s, exc, pair.id)
    assert s.get(ProviderIncident, info["incident_id"]).resolved_at
    assert not retry_pending(s, pair, run)


@pytest.mark.parametrize(
    "exc,kind",
    [
        (RateLimited(1200), "rate_limit"),
        (NetworkFailure("timeout"), "network"),
        (ProviderUnavailable("503"), "temporary"),
        (http_failure(403), "permissions"),
        (http_failure(403, write=True), "read_only"),
        (http_failure(404), "missing_resource"),
    ],
)
def test_categories_retry_and_resource_scoped_recovery(context, exc, kind):
    s, accounts, pair = context
    run = failure(s, accounts[1], pair, exc)
    incident = active_incidents(s, pair)[0]
    assert incident.category == kind
    assert retry_pending(s, pair, run)
    assert pair_health(s, pair, run)[0] != "Connection needs attention"
    assert "Reconnect" not in pair_health(s, pair, run)[1]
    if kind == "rate_limit":
        assert utc(incident.retry_at) > datetime.now(UTC) + timedelta(seconds=1190)
    verified(s, accounts[1].id, datetime.now(UTC), operation="list_playlists")
    assert active_incidents(s, pair)
    verified(
        s,
        accounts[1].id,
        datetime.now(UTC),
        operation="get_playlist",
        resource="synthetic-playlist",
    )
    assert not active_incidents(s, pair)


class SyntheticProvider:
    def __init__(self, name):
        self.name = name
        self.error = None
        self.reads = 0

    def get_playlist(self, pid):
        self.reads += 1
        if self.error:
            raise self.error
        return ProviderPlaylist(pid, "Synthetic", ())


@pytest.mark.parametrize("index", [0, 1])
def test_coordinator_failure_recovery_and_next_tick_no_duplicate_jobs(context, monkeypatch, index):
    s, accounts, pair = context
    providers = {a.id: SyntheticProvider(a.provider_name) for a in accounts}
    settings = Settings(credential_encryption_key=Fernet.generate_key().decode())
    coordinator = SyncCoordinator(s, settings, lambda a, _: providers[a.id])
    monkeypatch.setattr(coordinator, "_credentials", lambda _: {})
    coordinator.accept_current_state(pair)
    settings.automatic_sync_bindings = [automation_binding(pair)]
    providers[accounts[index].id].error = AuthorizationRequired("raw secret")
    with pytest.raises(AuthorizationRequired):
        coordinator.prepare_review(pair)
    run = SyncRunRepository(s).latest_for_pair(pair.id)
    assert json.loads(run.summary_json)["provider"] == accounts[index].provider_name
    reads = sum(p.reads for p in providers.values())
    run_automatic_pair(coordinator, pair)
    assert sum(p.reads for p in providers.values()) == reads
    providers[accounts[index].id].error = None
    for _ in range(5):
        verified(s, accounts[index].id, datetime.now(UTC))
    s.commit()
    lease = acquire_pair_lease(s, pair.id)
    try:
        with pytest.raises(PairOperationBusy):
            run_automatic_pair(coordinator, pair)
    finally:
        lease.release()
    assert run_automatic_pair(coordinator, pair) == "up to date"
    assert run_automatic_pair(coordinator, pair) == "up to date"
    assert len(list(s.scalars(select(SyncRun).where(SyncRun.status == "planned")))) == 1
    assert s.get(SyncRun, run.id).status == "authorization_required"


@pytest.mark.parametrize("provider", ["spotify", "youtube_music"])
@pytest.mark.parametrize("second_status", [200, 401])
def test_401_renews_once_before_reconnect(provider, second_status):
    calls = []
    refreshes = []

    def request(req):
        calls.append(req)
        return httpx.Response(
            401 if len(calls) == 1 else second_status, json={"items": [], "next": None}
        )

    http = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(request))

    def refresh(token):
        refreshes.append(token)
        return "new"

    if provider == "spotify":
        client = SpotifyProvider("old", http)
        client.set_token_refresher(refresh)
        operation = client.list_playlists
    else:
        client = _YouTubeDataApiClient("old", http)
        client._token_refresher = refresh

        def operation():
            return client._request("GET", "/playlists")

    if second_status == 401:
        with pytest.raises(AuthorizationRequired) as caught:
            operation()
        assert hasattr(caught.value, "operation_started_at")
    else:
        operation()
    assert refreshes == ["old"]
    assert len(calls) == 2
    assert calls[1].headers["Authorization"] == "Bearer new"


@pytest.mark.parametrize(
    "status,reason,kind",
    [
        (400, "invalid_grant", "authentication"),
        (403, "insufficientPermissions", "permissions"),
        (403, "quotaExceeded", "rate_limit"),
        (429, None, "rate_limit"),
        (503, None, "temporary"),
    ],
)
def test_oauth_and_data_errors_are_structured(status, reason, kind):
    assert category(http_failure(status, reason=reason, token=True)) == kind
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": reason}, headers={"Retry-After": "100"})
        )
    )
    with pytest.raises(Exception) as caught:
        SpotifyOAuthService(
            SpotifyOAuthConfig("id", "secret", "http://localhost"), http
        ).refresh_token("private")
    assert category(caught.value) == kind
    assert "private" not in str(caught.value)


def test_google_refresh_network_and_revocation_are_distinct():
    class Google:
        def refresh_token(self, _):
            raise requests.Timeout("raw token must not be exposed")

    with pytest.raises(NetworkFailure):
        YouTubeMusicAuthService("id", "secret", Google()).refresh_token("token")
    Google.refresh_token = lambda *_: {"error": "invalid_grant", "error_description": "private"}
    with pytest.raises(AuthorizationRequired) as caught:
        YouTubeMusicAuthService("id", "secret", Google()).refresh_token("token")
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("index", [0, 1])
def test_rotated_refresh_token_and_concurrent_reconnect_are_preserved(context, index):
    s, accounts, _ = context
    account = accounts[index]
    key = Fernet.generate_key().decode()
    settings = Settings(
        credential_encryption_key=key,
        spotify_client_id="id",
        spotify_client_secret="secret",
        ytmusic_client_id="id",
        ytmusic_client_secret="secret",
    )
    repo = ProviderAccountRepository(s, CredentialCipher(key))
    expired = {
        "access_token": "old",
        "refresh_token": "old-refresh",
        "expires_at": 1,
        "auth_scheme": "ytmusicapi_oauth",
    }
    if index == 0:
        expired["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    repo.save_credentials(account, expired)
    s.commit()

    class Refresh:
        def __init__(self, *_):
            pass

        def refresh_token(self, token):
            return {
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 3600,
                "expires_at": int(datetime.now(UTC).timestamp()) + 3600,
            }

    data = credentials_for(s, settings, account, spotify_service=Refresh, youtube_service=Refresh)
    assert data["refresh_token"] == "rotated"
    assert repo.load_credentials(account)["refresh_token"] == "rotated"
    assert "rotated" not in account.credentials_ciphertext
    # Simulate reconnect in another DB session during the network exchange.
    account_id = account.id

    class Concurrent(Refresh):
        def refresh_token(self, token):
            with Session(s.get_bind()) as other:
                current = other.get(ProviderAccount, account_id)
                ProviderAccountRepository(other, CredentialCipher(key)).save_credentials(
                    current,
                    {**data, "access_token": "reconnected", "refresh_token": "reconnected-refresh"},
                )
                verified(other, account_id, datetime.now(UTC))
                other.commit()
            return super().refresh_token(token)

    result = credentials_for(
        s,
        settings,
        account,
        rejected_token="new",
        spotify_service=Concurrent,
        youtube_service=Concurrent,
    )
    assert result["refresh_token"] == "reconnected-refresh"
    assert result["access_token"] == "reconnected"


def test_refresh_lease_prevents_overlapping_refresh_and_survives_restart(context):
    s, accounts, _ = context
    account = accounts[0]
    key = Fernet.generate_key().decode()
    repo = ProviderAccountRepository(s, CredentialCipher(key))
    repo.save_credentials(
        account, {"access_token": "old", "refresh_token": "old", "expires_at": "2000-01-01"}
    )
    account.refresh_lock_token = "another-worker"
    account.refresh_lock_until = datetime.now(UTC) + timedelta(minutes=1)
    s.commit()
    with Session(s.get_bind()) as other:
        with pytest.raises(ProviderUnavailable, match="already running"):
            credentials_for(
                other,
                Settings(credential_encryption_key=key),
                other.get(ProviderAccount, account.id),
            )


def test_public_search_success_does_not_prove_authorization(context):
    s, accounts, pair = context
    run = failure(s, accounts[1], pair)

    class Search:
        name = "youtube_music"

        def search_track(self, _):
            return None

    ObservedProvider(Search(), s, accounts[1].id).search_track(None)
    assert retry_pending(s, pair, run)


def test_pairs_and_activity_show_recovery_without_deleting_history(tmp_path, monkeypatch):
    from test_security_boundaries import _complete_setup, _isolated_client

    from ops.api import routes

    client, factory, settings = _isolated_client(tmp_path, monkeypatch)
    with client:
        _complete_setup(client, settings)
        with factory() as s:
            accounts = [
                ProviderAccount(provider_name=n, external_account_id=n)
                for n in ("spotify", "youtube_music")
            ]
            s.add_all(accounts)
            s.flush()
            for a in accounts:
                ProviderAccountRepository(
                    s, CredentialCipher(settings.credential_encryption_key)
                ).save_credentials(a, {"access_token": "synthetic"})
            pair = SyncPair(
                source_account_id=accounts[0].id,
                target_account_id=accounts[1].id,
                source_playlist_id="spotify:source",
                target_playlist_id="youtube_music:target",
            )
            s.add(pair)
            s.flush()
            run = failure(s, accounts[1], pair)
            run_id = run.id

        class Picker:
            def __init__(self, name):
                self.name = name

            def list_playlists(self):
                return [ProviderPlaylist(f"{self.name}:source", "Synthetic", ())]

        monkeypatch.setattr(routes, "load_app_settings", lambda _: settings)
        monkeypatch.setattr(
            routes, "provider_for_account", lambda s, st, a: Picker(a.provider_name)
        )
        page = client.get("/pairs")
        assert page.status_code == 200
        assert "Connection needs attention" not in page.text
        assert "connected and verified" in page.text
        assert "Synthetic" in page.text
        history = client.get("/runs")
        assert history.status_code == 200
        assert f"#{run_id}" in history.text
        assert "Resolved" in history.text
        assert "Authorization Required" in history.text
        assert "Youtube Music" in history.text


def test_migration_upgrade_downgrade_preserves_account_and_history(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from ops import config

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'migration.db'}")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0012_sync_mode")
    engine = create_engine(settings.database_url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO provider_accounts (id,provider_name,external_account_id) "
                "VALUES (1,'spotify','synthetic')"
            )
        )
        c.execute(text("INSERT INTO sync_runs (id,status) VALUES (1,'authorization_required')"))
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT verified_at FROM provider_accounts WHERE id=1")).scalar() is None
        )
        assert (
            c.execute(text("SELECT status FROM sync_runs WHERE id=1")).scalar()
            == "authorization_required"
        )
        assert c.execute(text("SELECT count(*) FROM provider_incidents")).scalar() == 0
    command.downgrade(cfg, "0012_sync_mode")
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT status FROM sync_runs WHERE id=1")).scalar()
            == "authorization_required"
        )
        assert (
            c.execute(text("SELECT provider_name FROM provider_accounts WHERE id=1")).scalar()
            == "spotify"
        )
    command.upgrade(cfg, "head")
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    kind != "table" or name in {"provider_accounts", "provider_incidents"}
                )
            },
        )
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()


def test_final_401_after_successful_refresh_is_still_unresolved(context):
    s, accounts, pair = context
    a = accounts[0]

    def refresh(_):
        verified(s, a.id, datetime.now(UTC), operation="refresh")
        return "new-but-rejected"

    provider = SpotifyProvider(
        "old",
        httpx.Client(
            base_url="https://example.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(401)),
        ),
    )
    provider.set_token_refresher(refresh)
    observed = ObservedProvider(provider, s, a.id)
    with pytest.raises(AuthorizationRequired) as caught:
        observed.list_playlists()
    info = record_failure(s, caught.value, pair.id)
    s.commit()
    assert s.get(ProviderIncident, info["incident_id"]).resolved_at is None


def test_retry_after_http_date_and_temporary_backoff(context):
    from email.utils import format_datetime

    from ops.providers.errors import retry_after

    s, accounts, pair = context
    assert retry_after(format_datetime(datetime.now(UTC) + timedelta(minutes=30))) >= 1790
    run = None
    previous = 0
    for _ in range(9):
        run = failure(s, accounts[0], pair, NetworkFailure("timeout"))
        incident = active_incidents(s, pair)[0]
        seconds = (utc(incident.retry_at) - datetime.now(UTC)).total_seconds()
        assert seconds >= previous - 1
        assert seconds <= 3600
        previous = seconds
    assert retry_pending(s, pair, run)


def test_successful_refresh_clears_its_network_backoff_not_other_operations(context):
    s, accounts, pair = context
    run = failure(s, accounts[0], pair, NetworkFailure("timeout"), operation="refresh")
    incident = active_incidents(s, pair)[0]
    incident.resource = None
    s.commit()
    verified(s, accounts[0].id, datetime.now(UTC), operation="refresh")
    s.commit()
    assert not retry_pending(s, pair, run)
    run = failure(s, accounts[0], pair, http_failure(403, write=True), operation="add_tracks")
    verified(s, accounts[0].id, datetime.now(UTC), operation="refresh")
    assert retry_pending(s, pair, run)


def test_refresh_expired_lease_does_not_return_stale_token(context):
    s, accounts, _ = context
    key = Fernet.generate_key().decode()
    account = accounts[0]
    settings = Settings(
        credential_encryption_key=key, spotify_client_id="id", spotify_client_secret="secret"
    )
    repo = ProviderAccountRepository(s, CredentialCipher(key))
    repo.save_credentials(
        account, {"access_token": "old", "refresh_token": "old", "expires_at": "2000-01-01"}
    )
    s.commit()

    class SlowRefresh:
        def __init__(self, *_):
            pass

        def refresh_token(self, _):
            account.refresh_lock_until = datetime.now(UTC) - timedelta(seconds=1)
            s.commit()
            return {"access_token": "too-late", "expires_in": 3600}

    with pytest.raises(ProviderUnavailable, match="lease expired"):
        credentials_for(s, settings, account, spotify_service=SlowRefresh)
    assert repo.load_credentials(account)["access_token"] == "old"
    assert account.refresh_lock_token is None


def test_malformed_provider_reason_does_not_crash_classifier():
    assert category(http_failure(403, reason={"unexpected": "shape"})) == "permissions"


def test_refresh_failure_from_playlist_http_request_is_account_scoped(context):
    s, accounts, pair = context

    class Provider:
        name = "spotify"

        def get_playlist(self, playlist_id):
            exc = NetworkFailure("refresh timeout")
            exc.operation = "refresh"
            raise exc

    with pytest.raises(NetworkFailure) as caught:
        ObservedProvider(Provider(), s, accounts[0].id).get_playlist(playlist_id="synthetic")
    info = record_failure(s, caught.value, pair.id)
    s.commit()
    incident = s.get(ProviderIncident, info["incident_id"])
    assert incident.resource is None
    verified(s, accounts[0].id, datetime.now(UTC), operation="refresh")
    s.commit()
    assert not active_incidents(s, pair)


@pytest.mark.parametrize(
    "status,kind",
    [(401, "authentication"), (403, "permissions"), (429, "rate_limit"), (503, "temporary")],
)
def test_google_oauth_http_status_preserved_before_sdk_parsing(monkeypatch, status, kind):
    from ops.auth.youtube_music import OAuthSession

    response = requests.Response()
    response.status_code = status
    response._content = b'{"error":"provider_error"}'
    response.headers["Retry-After"] = "1300"

    def request(self, method, url, **kwargs):
        assert kwargs["timeout"] == 20
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    service = YouTubeMusicAuthService("synthetic-id", "synthetic-secret")
    assert isinstance(service.credentials._session, OAuthSession)
    with pytest.raises(Exception) as caught:
        service.refresh_token("synthetic-refresh")
    assert category(caught.value) == kind
    if status == 429:
        assert caught.value.retry_after_seconds == 1300
