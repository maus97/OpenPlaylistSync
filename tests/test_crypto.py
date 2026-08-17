import pytest
from cryptography.fernet import Fernet

from ops.security.crypto import CredentialCipher, CredentialEncryptionError


def test_credentials_round_trip_without_plaintext_ciphertext() -> None:
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    payload = {"access_token": "secret-token", "refresh_token": "refresh-token"}

    ciphertext = cipher.encrypt(payload)

    assert "secret-token" not in ciphertext
    assert cipher.decrypt(ciphertext) == payload


def test_tampered_credentials_are_rejected() -> None:
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))

    with pytest.raises(CredentialEncryptionError):
        cipher.decrypt("not-valid-ciphertext")
