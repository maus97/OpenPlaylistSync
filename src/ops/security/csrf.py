"""Small session-bound CSRF protection for browser form submissions."""

import secrets
from typing import Annotated

from fastapi import Form, HTTPException, Request, status


def csrf_context(request: Request) -> dict[str, str]:
    """Provide one stable opaque token to server-rendered templates."""

    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return {"csrf_token": token}


def require_csrf(request: Request, csrf_token: Annotated[str | None, Form()] = None) -> None:
    """Reject state-changing form posts without the current session token."""

    expected = request.session.get("csrf_token")
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid form security token"
        )
