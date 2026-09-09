"""Create provider adapters from encrypted account payloads."""

from typing import Any

from ops.models import ProviderAccount
from ops.providers.base import MusicProvider
from ops.providers.spotify import SpotifyProvider
from ops.providers.youtube_music import YTMusicApiProvider


def create_provider(account: ProviderAccount, credentials: dict[str, Any]) -> MusicProvider:
    """Build a provider adapter without exposing credentials to callers."""

    if account.provider_name == "spotify":
        return SpotifyProvider(access_token=credentials.get("access_token"))
    if account.provider_name == "youtube_music":
        # The OAuth client configuration is added only to the short-lived
        # in-memory payload by the coordinator/routes. It is never persisted
        # alongside the account token or exposed by this factory.
        return YTMusicApiProvider(
            credentials,
            client_id=credentials.get("_ytmusic_client_id"),
            client_secret=credentials.get("_ytmusic_client_secret"),
        )
    raise ValueError(f"unsupported provider: {account.provider_name}")
