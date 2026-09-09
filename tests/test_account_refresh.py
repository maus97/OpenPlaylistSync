from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.api import routes
from ops.auth.youtube_music import YOUTUBE_MUSIC_AUTH_SCHEME
from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount
from ops.providers.spotify import SpotifyProvider
from ops.providers.youtube_music import YTMusicApiProvider
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import ProviderAccountRepository


def test_playlist_picker_refreshes_expired_spotify_token(monkeypatch) -> None:
    class FakeSpotifyOAuthService:
        def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
            self.config = config

        def refresh_token(self, refresh_token: str) -> dict[str, object]:
            assert refresh_token == "refresh-token"
            return {"access_token": "new-access-token", "expires_in": 3600}

    monkeypatch.setattr(routes, "SpotifyOAuthService", FakeSpotifyOAuthService)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        credential_encryption_key=key,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
    )
    with Session(engine) as session:
        account = ProviderAccount(provider_name="spotify", external_account_id="user")
        session.add(account)
        session.flush()
        repository = ProviderAccountRepository(session, CredentialCipher(key))
        repository.save_credentials(
            account,
            {
                "access_token": "old-access-token",
                "refresh_token": "refresh-token",
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )
        session.commit()

        provider = routes.provider_for_account(session, settings, account)

        assert isinstance(provider, SpotifyProvider)
        assert provider.access_token == "new-access-token"
        assert repository.load_credentials(account)["refresh_token"] == "refresh-token"
    engine.dispose()


def test_playlist_picker_refreshes_expired_ytmusicapi_token(monkeypatch) -> None:
    class FakeYouTubeMusicAuthService:
        def __init__(self, client_id: str, client_secret: str) -> None:
            assert (client_id, client_secret) == ("client-id", "client-secret")

        def refresh_token(self, refresh_token: str) -> dict[str, object]:
            assert refresh_token == "refresh-token"
            return {
                "auth_scheme": YOUTUBE_MUSIC_AUTH_SCHEME,
                "access_token": "new-access-token",
                "refresh_token": "refresh-token",
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
                "expires_at": 4_102_444_800,
                "expires_in": 3600,
            }

    monkeypatch.setattr(routes, "YouTubeMusicAuthService", FakeYouTubeMusicAuthService)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        credential_encryption_key=key,
        ytmusic_client_id="client-id",
        ytmusic_client_secret="client-secret",
    )
    with Session(engine) as session:
        account = ProviderAccount(provider_name="youtube_music", external_account_id="default")
        session.add(account)
        session.flush()
        repository = ProviderAccountRepository(session, CredentialCipher(key))
        repository.save_credentials(
            account,
            {
                "auth_scheme": YOUTUBE_MUSIC_AUTH_SCHEME,
                "access_token": "old-access-token",
                "refresh_token": "refresh-token",
                "expires_at": 1,
            },
        )
        session.commit()

        provider = routes.provider_for_account(session, settings, account)

        assert isinstance(provider, YTMusicApiProvider)
        assert provider._credentials["access_token"] == "new-access-token"
        assert repository.load_credentials(account)["refresh_token"] == "refresh-token"
    engine.dispose()
