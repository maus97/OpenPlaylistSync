"""Configuration loaded from environment variables and local runtime storage."""

import secrets
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration with an OPS_ environment-variable prefix."""

    app_name: str = "Open Playlist Sync"
    environment: str = "development"
    database_url: str = "sqlite:///./data/ops.db"
    data_dir: str = "data"
    log_level: str = "INFO"
    credential_encryption_key: str | None = None
    session_secret: str | None = None
    session_cookie_secure: bool | None = None
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

    def ensure_runtime_secrets(self) -> "Settings":
        """Create persistent local secrets when deployment variables are absent.

        This keeps first-run setup inside the GUI. The generated files live beside
        the SQLite database in the Docker volume and are never shown in the UI.
        Operators can still provide externally managed values through the
        environment for deployments that require that control.
        """

        runtime_dir = Path(self.data_dir)
        self.credential_encryption_key = _read_or_create_secret(
            runtime_dir / ".ops-credential-key",
            self.credential_encryption_key,
            lambda: Fernet.generate_key().decode("ascii"),
        )
        self.session_secret = _read_or_create_secret(
            runtime_dir / ".ops-session-secret",
            self.session_secret,
            lambda: secrets.token_urlsafe(32),
        )
        return self


def _read_or_create_secret(path: Path, configured: str | None, generator: Callable[[], str]) -> str:
    """Return a configured secret or create a persistent local replacement."""

    if configured and configured.strip():
        return configured.strip()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    value = generator()
    path.write_text(f"{value}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object."""

    return Settings().ensure_runtime_secrets()
