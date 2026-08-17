from ops.providers.base import MusicProvider
from ops.providers.spotify import SpotifyProvider
from ops.providers.youtube_music import YouTubeMusicProvider


def test_initial_provider_seams_share_the_common_contract() -> None:
    assert isinstance(SpotifyProvider(), MusicProvider)
    assert isinstance(YouTubeMusicProvider(), MusicProvider)
