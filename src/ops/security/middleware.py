"""Authentication, request-boundary, and browser response middleware."""

from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ops.db import SessionLocal
from ops.security.local_auth import administrator


@dataclass
class RuntimeSecurityMode:
    """Mutable startup state shared by cookie and response-header handling."""

    https_enabled: bool


class LocalAuthenticationMiddleware(BaseHTTPMiddleware):
    """Require a signed local session everywhere except setup, login, and health checks."""

    PUBLIC_PATHS = {"/healthz", "/auth/setup", "/auth/login"}

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith("/static/") or path in self.PUBLIC_PATHS:
            return await call_next(request)
        with SessionLocal() as session:
            record = administrator(session)
        if record is None:
            return RedirectResponse("/auth/setup", status_code=303)
        if (
            not request.session.get("local_admin_authenticated")
            or request.session.get("local_admin_session_generation") != record.session_generation
        ):
            return RedirectResponse("/auth/login", status_code=303)
        return await call_next(request)


class RequestBodyLimitMiddleware:
    """Reject oversized browser writes before form parsing or password hashing."""

    def __init__(self, app: ASGIApp, max_bytes: int = 65_536) -> None:
        self.app = app
        self.max_bytes = max(1024, max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b"Request body is too large."
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Apply a consistent browser policy to success, redirect, and error responses."""

    CONTENT_SECURITY_POLICY = (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'"
    )

    def __init__(self, app: ASGIApp, *, security_mode: RuntimeSecurityMode) -> None:
        self.app = app
        self.security_mode = security_mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}

                def add(name: bytes, value: bytes) -> None:
                    if name not in names:
                        headers.append((name, value))

                add(b"content-security-policy", self.CONTENT_SECURITY_POLICY.encode("ascii"))
                add(b"x-frame-options", b"DENY")
                add(b"x-content-type-options", b"nosniff")
                add(b"referrer-policy", b"same-origin")
                add(b"permissions-policy", b"camera=(), microphone=(), geolocation=()")
                add(b"cross-origin-opener-policy", b"same-origin")
                if not path.startswith("/static/") and path != "/healthz":
                    add(b"cache-control", b"no-store")
                    add(b"pragma", b"no-cache")
                if self.security_mode.https_enabled:
                    add(b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    headers = [
                        (
                            name,
                            value + b"; Secure"
                            if name.lower() == b"set-cookie"
                            and value.lower().startswith(b"ops_session=")
                            and b"; secure" not in value.lower()
                            else value,
                        )
                        for name, value in headers
                    ]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, add_headers)
