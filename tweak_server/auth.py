"""Simple bearer-token auth for the tweak server.

For MVP (internal network) we don't need session/cookie machinery. The browser
sends an ``X-Tweak-Token`` header; we compare it (constant-time) to
``TWEAK_SERVER_BEARER`` env var.

If ``TWEAK_SERVER_BEARER`` is empty, auth is disabled (development convenience).
A loud warning is logged so it can't slip into a public deployment by accident.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException, status

_log = logging.getLogger("tweak_server.auth")

TWEAK_SERVER_BEARER = os.environ.get("TWEAK_SERVER_BEARER", "").strip()


def _check_token(provided: str | None) -> None:
    """Raise 401 if a token is required and the provided one is wrong."""
    if not TWEAK_SERVER_BEARER:
        # Auth disabled — fine for internal dev. Logged once at app startup.
        return
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Tweak-Token header",
        )
    # constant-time compare
    if not hmac.compare_digest(provided.encode("utf-8"), TWEAK_SERVER_BEARER.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Tweak-Token",
        )


async def require_token(x_tweak_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency. Use in route signatures: ``Depends(require_token)``."""
    _check_token(x_tweak_token)