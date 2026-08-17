"""Authenticated encryption for provider credentials."""

import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(ValueError):
    """Raised when credential encryption configuration or ciphertext is invalid."""


class CredentialCipher:
    """Encrypt JSON credential payloads using an operator-managed Fernet key."""

    def __init__(self, key: str) -> None:
        if not key:
            raise CredentialEncryptionError("OPS_CREDENTIAL_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise CredentialEncryptionError(
                "credential encryption key is not valid Fernet data"
            ) from exc

    def encrypt(self, payload: Mapping[str, Any]) -> str:
        """Return ciphertext without exposing the payload in logs or errors."""

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(encoded).decode("ascii")

    def decrypt(self, ciphertext: str) -> dict[str, Any]:
        """Decrypt a credential payload and reject malformed or tampered data."""

        try:
            raw = self._fernet.decrypt(ciphertext.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
            raise CredentialEncryptionError("credential ciphertext could not be decrypted") from exc
        if not isinstance(payload, dict):
            raise CredentialEncryptionError("credential payload must be a JSON object")
        return payload
