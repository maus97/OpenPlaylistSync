from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.configuration import load_app_settings, load_saved_settings, save_app_settings
from ops.db import Base
from ops.models import AppConfiguration


def test_runtime_secrets_are_generated_and_reused(tmp_path) -> None:
    first = Settings(
        data_dir=str(tmp_path),
        credential_encryption_key="",
        session_secret="",
    ).ensure_runtime_secrets()
    second = Settings(
        data_dir=str(tmp_path),
        credential_encryption_key="",
        session_secret="",
    ).ensure_runtime_secrets()

    assert first.credential_encryption_key
    assert first.session_secret
    assert second.credential_encryption_key == first.credential_encryption_key
    assert second.session_secret == first.session_secret


def test_gui_settings_are_encrypted_and_override_defaults() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    base = Settings(
        credential_encryption_key=Fernet.generate_key().decode("ascii"),
        session_secret="session-secret",
        spotify_client_id="environment-id",
    )

    with Session(engine) as session:
        save_app_settings(
            session,
            {
                "spotify_client_id": "gui-id",
                "spotify_client_secret": "gui-secret",
            },
            base,
        )
        session.commit()

        configuration = session.get(AppConfiguration, 1)
        assert configuration is not None
        assert "gui-secret" not in configuration.settings_ciphertext
        assert load_saved_settings(session, base) == {
            "spotify_client_id": "gui-id",
            "spotify_client_secret": "gui-secret",
        }
        assert load_app_settings(session, base).spotify_client_id == "gui-id"
