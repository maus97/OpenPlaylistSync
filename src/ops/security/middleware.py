"""Authentication gate for the local OPS browser application."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ops.db import SessionLocal
from ops.security.local_auth import administrator


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
