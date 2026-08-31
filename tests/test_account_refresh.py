from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.api import routes
from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount
from ops.providers.spotify import SpotifyProvider
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
