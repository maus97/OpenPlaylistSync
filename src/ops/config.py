"""Configuration loaded from environment variables and optional local .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration with an OPS_ environment-variable prefix."""

    app_name: str = "Open Playlist Sync"
    environment: str = "development"
    database_url: str = "sqlite:///./data/ops.db"
    log_level: str = "INFO"
    credential_encryption_key: str | None = None
    session_secret: str | None = None
    scheduler_enabled: bool = False
    sync_interval_minutes: int = 60
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_redirect_uri: str = "http://127.0.0.1:8000/auth/spotify/callback"
    ytmusic_client_id: str | None = None
    ytmusic_client_secret: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPS_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object."""

    return Settings()
