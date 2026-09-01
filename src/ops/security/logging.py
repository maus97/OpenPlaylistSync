"""Logging safeguards for URL-borne OAuth artifacts."""

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_PARTS = ("code", "state", "token", "secret", "assertion")


def redact_query(url: str) -> str:
    """Redact credential-like query values while retaining useful route context."""

    parsed = urlsplit(url)
    if not parsed.query:
        return url
    redacted = [
        (
            key,
            "REDACTED" if any(part in key.casefold() for part in _SENSITIVE_QUERY_PARTS) else value,
        )
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted), parsed.fragment)
    )


class SensitiveQueryFilter(logging.Filter):
    """Sanitize Uvicorn's positional full-path argument before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            arguments[2] = redact_query(str(arguments[2]))
            record.args = tuple(arguments)
        return True


def install_sensitive_query_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SensitiveQueryFilter) for item in logger.filters):
        logger.addFilter(SensitiveQueryFilter())
