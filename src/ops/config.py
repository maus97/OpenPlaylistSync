"""Configuration loaded from environment variables and local runtime storage."""

import os
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
    secret_dir: str | None = None
    log_level: str = "INFO"
    credential_encryption_key: str | None = None
    credential_encryption_key_file: str | None = None
    session_secret: str | None = None
    session_secret_file: str | None = None
    session_cookie_secure: bool | None = None
    bootstrap_token: str | None = None
    bootstrap_token_file: str | None = None
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    trusted_proxy_ips: str = ""
    max_request_body_bytes: int = 65_536
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

        This keeps first-run setup inside the GUI. The generated files live in
        the configured secret directory and are never shown in the UI.
        Operators can still provide externally managed values through the
        environment for deployments that require that control.
        """

        data_dir = Path(self.data_dir)
        runtime_dir = Path(self.secret_dir or self.data_dir)
        _secure_directory(data_dir)
        _secure_directory(runtime_dir)
        self.credential_encryption_key = _read_or_create_secret(
            runtime_dir / ".ops-credential-key",
            _configured_secret(
                self.credential_encryption_key,
                self.credential_encryption_key_file,
            ),
            lambda: Fernet.generate_key().decode("ascii"),
            legacy_path=data_dir / ".ops-credential-key",
        )
        self.session_secret = _read_or_create_secret(
            runtime_dir / ".ops-session-secret",
            _configured_secret(self.session_secret, self.session_secret_file),
            lambda: secrets.token_urlsafe(32),
            legacy_path=data_dir / ".ops-session-secret",
        )
        return self

    @property
    def allowed_host_list(self) -> list[str]:
        """Return a normalized non-empty TrustedHost allowlist."""

        hosts = [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]
        return hosts or ["127.0.0.1", "localhost", "testserver"]


def _secure_directory(path: Path) -> None:
    """Create a runtime directory with private POSIX permissions where supported."""

    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _configured_secret(value: str | None, file_name: str | None) -> str | None:
    """Read one operator-managed secret without falling back after an invalid file."""

    if value and value.strip():
        return value.strip()
    if not file_name or not file_name.strip():
        return None
    path = Path(file_name.strip())
    try:
        if path.stat().st_size > 4096:
            raise ValueError(f"secret file is unexpectedly large: {path}")
        configured = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"configured secret file could not be read: {path}") from exc
    if not configured:
        raise ValueError(f"configured secret file is empty: {path}")
    return configured


def _read_or_create_secret(
    path: Path,
    configured: str | None,
    generator: Callable[[], str],
    *,
    legacy_path: Path | None = None,
) -> str:
    """Return a configured secret or create a persistent local replacement."""

    if configured and configured.strip():
        return configured.strip()
    _secure_directory(path.parent)
    existing = _read_runtime_secret(path)
    if existing:
        return existing
    if legacy_path is not None and legacy_path != path:
        existing = _read_runtime_secret(legacy_path)
        if existing:
            _create_secret_file(path, existing)
            return existing
    value = generator().strip()
    try:
        _create_secret_file(path, value)
    except FileExistsError:
        # Another process won first-start initialization. Reuse only its complete value.
        existing = _read_runtime_secret(path)
        if not existing:
            raise RuntimeError(f"runtime secret was created without a value: {path}") from None
        return existing
    return value


def _read_runtime_secret(path: Path) -> str:
    try:
        if path.stat().st_size > 4096:
            raise ValueError(f"runtime secret file is unexpectedly large: {path}")
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    if not value:
        raise ValueError(f"runtime secret file is empty: {path}")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _create_secret_file(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object."""

    return Settings().ensure_runtime_secrets()
