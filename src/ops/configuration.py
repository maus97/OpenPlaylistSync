"""Encrypted GUI configuration layered over deployment defaults."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from ops.config import Settings, get_settings
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import AppConfigurationRepository

GUI_SETTING_KEYS = frozenset(
    {
        "spotify_client_id",
        "spotify_client_secret",
        "spotify_redirect_uri",
        "ytmusic_client_id",
        "ytmusic_client_secret",
        "session_cookie_secure",
        "scheduler_enabled",
        "sync_interval_minutes",
    }
)


def load_saved_settings(session: Session, base: Settings | None = None) -> dict[str, Any]:
    """Load encrypted settings entered in the GUI without exposing ciphertext."""

    base = base or get_settings()
    cipher = CredentialCipher(base.credential_encryption_key or "")
    return AppConfigurationRepository(session, cipher).load()


def load_app_settings(session: Session, base: Settings | None = None) -> Settings:
    """Return deployment defaults overlaid with encrypted GUI settings."""

    base = base or get_settings()
    saved = load_saved_settings(session, base)
    updates = {key: value for key, value in saved.items() if key in GUI_SETTING_KEYS}
    if base.session_cookie_secure is not None:
        # An explicit deployment value is the recovery/administrative override.
        updates.pop("session_cookie_secure", None)
    if not updates:
        return base
    return Settings(**{**base.model_dump(), **updates})


def save_app_settings(
    session: Session, values: Mapping[str, Any], base: Settings | None = None
) -> None:
    """Encrypt and save only supported GUI settings."""

    base = base or get_settings()
    cipher = CredentialCipher(base.credential_encryption_key or "")
    filtered = {key: value for key, value in values.items() if key in GUI_SETTING_KEYS}
    AppConfigurationRepository(session, cipher).save(filtered)
