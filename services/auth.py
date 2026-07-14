"""
services/auth.py — Single-password HTTPBasic auth.

If APP_PASSWORD is set in the environment, every request (except /health,
/docs, /openapi.json, and /favicon) requires HTTPBasic. The username is
ignored — only the password is checked. This is the bare minimum protection
needed before exposing the app on a public URL.

If APP_PASSWORD is empty or unset, auth is DISABLED (development mode).
"""

import logging
import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

log = logging.getLogger(__name__)

# Paths that bypass auth — health check (for Render) and API docs
PUBLIC_PATHS = (
    "/health",
    "/favicon.ico",
)

# Prefixes that bypass auth. /static/signatures/ must be public so email
# clients (Gmail, Outlook) can fetch signature logos from the emails we send;
# those fetches don't carry Basic Auth headers.
PUBLIC_PREFIXES = (
    "/static/signatures/",
)


def _expected_password() -> Optional[str]:
    pw = os.getenv("APP_PASSWORD", "").strip()
    return pw if pw else None


def _is_public(path: str) -> bool:
    # /health and /favicon.ico always public
    if path in PUBLIC_PATHS:
        return True
    # Public prefixes (signature logos, etc.)
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _check_basic_auth(header_value: str, expected: str) -> bool:
    """Parse 'Basic base64(user:pass)' and verify password using constant-time compare."""
    import base64
    try:
        scheme, encoded = header_value.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded).decode("utf-8")
        _user, _, password = decoded.partition(":")
        return secrets.compare_digest(password, expected)
    except Exception:
        return False


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforces HTTPBasic if APP_PASSWORD is set.
    Returns 401 with WWW-Authenticate so browsers pop a login dialog.
    """

    async def dispatch(self, request: Request, call_next):
        expected = _expected_password()
        if expected is None or _is_public(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header and _check_basic_auth(auth_header, expected):
            return await call_next(request)

        return Response(
            content="Authentication required",
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="Denali BD Automation"'},
        )
