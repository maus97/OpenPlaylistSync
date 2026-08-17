"""Create provider adapters from encrypted account payloads."""

from typing import Any

from ops.models import ProviderAccount
from ops.providers.demo import DemoProvider
from ops.providers.spotify import SpotifyProvider
from ops.providers.youtube_music import YouTubeMusicProvider


def create_provider(account: ProviderAccount, credentials: dict[str, Any]) -> object:
    """Build a provider adapter without exposing credentials to callers."""

    if account.provider_name == "spotify":
        return SpotifyProvider(access_token=credentials.get("access_token"))
    if account.provider_name == "youtube_music":
        return YouTubeMusicProvider(
            auth=credentials,
            oauth_client_id=credentials.get("_oauth_client_id"),
            oauth_client_secret=credentials.get("_oauth_client_secret"),
        )
    if account.provider_name == "demo_spotify":
        return DemoProvider("spotify")
    if account.provider_name == "demo_youtube_music":
        return DemoProvider("youtube_music")
    raise ValueError(f"unsupported provider: {account.provider_name}")
