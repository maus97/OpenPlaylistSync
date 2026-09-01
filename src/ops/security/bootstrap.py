"""One-time, out-of-band authorization for first-run administrator setup."""

import secrets
from pathlib import Path

from ops.config import Settings, _configured_secret, _read_or_create_secret, get_settings

BOOTSTRAP_TOKEN_MINIMUM_LENGTH = 32
BOOTSTRAP_TOKEN_FILE = ".ops-bootstrap-token"


class BootstrapAuthorizationError(ValueError):
    """Raised when first-run setup is not authorized by the operator token."""


def _token_path(settings: Settings) -> Path:
    return Path(settings.secret_dir or settings.data_dir) / BOOTSTRAP_TOKEN_FILE


def bootstrap_token(settings: Settings) -> str:
    """Return the configured token or atomically create a local one-time token."""

    configured = _configured_secret(settings.bootstrap_token, settings.bootstrap_token_file)
    token = _read_or_create_secret(
        _token_path(settings),
        configured,
        lambda: secrets.token_urlsafe(32),
    )
    if len(token) < BOOTSTRAP_TOKEN_MINIMUM_LENGTH:
        raise BootstrapAuthorizationError(
            f"the bootstrap token must contain at least {BOOTSTRAP_TOKEN_MINIMUM_LENGTH} characters"
        )
    return token


def verify_bootstrap_token(settings: Settings, submitted: str) -> None:
    """Fail closed before password hashing when the one-time token is absent or wrong."""

    if not submitted or len(submitted) > 512:
        raise BootstrapAuthorizationError("The setup code is not valid.")
    expected = bootstrap_token(settings)
    if not secrets.compare_digest(expected, submitted):
        raise BootstrapAuthorizationError("The setup code is not valid.")


def consume_bootstrap_token(settings: Settings) -> None:
    """Remove only an application-generated token after administrator creation."""

    if settings.bootstrap_token or settings.bootstrap_token_file:
        return
    path = _token_path(settings)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    """Print the one-time token to an operator-controlled terminal."""

    print(bootstrap_token(get_settings()))


if __name__ == "__main__":
    main()
