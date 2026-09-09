"""Disposable browser smoke-test app. Never load with a real database or credentials.

Run only in a separate container with OPS_ENVIRONMENT=browser-test,
OPS_DATABASE_URL=sqlite:////tmp/browser-sync.db and a distinct loopback port.
The pre-created synthetic operator password is Browser test playlist only!42.
All provider operations below are in-memory; no provider network calls exist.
"""

# ruff: noqa: E402
# Check the disposable environment before importing modules that initialize storage.

import os
from dataclasses import replace

if (
    os.environ.get("OPS_ENVIRONMENT") != "browser-test"
    or os.environ.get("OPS_DATABASE_URL") != "sqlite:////tmp/browser-sync.db"
):
    raise RuntimeError("This fixture requires its isolated, disposable test database")

from starlette.middleware.sessions import SessionMiddleware

from ops.api import routes
from ops.auth.youtube_music import YOUTUBE_MUSIC_AUTH_SCHEME
from ops.config import get_settings
from ops.db import Base, SessionLocal, engine
from ops.main import create_app
from ops.models import ProviderAccount, SyncPair
from ops.providers.spotify import SpotifyProvider
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.security.crypto import CredentialCipher
from ops.security.local_auth import create_administrator
from ops.storage.repositories import ProviderAccountRepository


class SyntheticProvider:
    resolve_search_candidates = staticmethod(SpotifyProvider.resolve_search_candidates)

    def __init__(self, name, tracks):
        self.name = name
        self.tracks = tracks

    def get_playlist(self, playlist_id):
        return ProviderPlaylist(playlist_id, "Synthetic browser test", tuple(self.tracks))

    def list_playlists(self):
        return [self.get_playlist(f"{self.name}:synthetic")]

    def search_track(self, track):
        return replace(track, provider_track_id=f"{self.name}:resolved")

    def add_tracks(self, playlist_id, tracks):
        self.tracks.extend(
            replace(
                track, title=f"Artist - {track.title} (Official Audio)", artists=("Artist - Topic",)
            )
            for track in tracks
        )

    def remove_tracks(self, playlist_id, tracks, **kwargs):
        raise AssertionError("The add-only browser scenario must never remove a track")


Base.metadata.create_all(engine)
with SessionLocal() as session:
    create_administrator(session, "Browser test playlist only!42")
    source = ProviderAccount(provider_name="spotify", external_account_id="synthetic-source")
    target = ProviderAccount(provider_name="youtube_music", external_account_id="synthetic-target")
    session.add_all((source, target))
    session.flush()
    credentials = ProviderAccountRepository(
        session, CredentialCipher(get_settings().credential_encryption_key)
    )
    credentials.save_credentials(source, {"access_token": "synthetic-not-a-real-token"})
    credentials.save_credentials(
        target,
        {
            "access_token": "synthetic-not-a-real-token",
            "auth_scheme": YOUTUBE_MUSIC_AUTH_SCHEME,
            "expires_at": 4102444800,
        },
    )
    session.add(
        SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:synthetic",
            target_playlist_id="youtube_music:synthetic",
        )
    )
    session.commit()

providers = {
    "spotify": SyntheticProvider(
        "spotify",
        [
            ProviderTrack("spotify:existing", "Afterglow", ("Ed Sheeran",), duration_ms=185000),
            ProviderTrack(
                "spotify:new", "Example Song - Acoustic", ("Artist",), duration_ms=180000
            ),
        ],
    ),
    "youtube_music": SyntheticProvider(
        "youtube_music",
        [
            ProviderTrack(
                "youtube_music:existing",
                "Ed Sheeran - Afterglow (Official Video)",
                ("Ed Sheeran",),
                duration_ms=185000,
            ),
        ],
    ),
}
routes.create_provider = lambda account, _: providers[account.provider_name]
app = create_app()
for middleware in app.user_middleware:
    if middleware.cls is SessionMiddleware:
        middleware.kwargs["session_cookie"] = "ops_browser_test_session"
