"""Request-scoped MCP session identity for streamable HTTP calls."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional


_mcp_session_id: ContextVar[Optional[str]] = ContextVar("mcp_session_id", default=None)
_mcp_request_id: ContextVar[Optional[str]] = ContextVar("mcp_request_id", default=None)


def set_mcp_session_id(value: Optional[str]):
    """Set the session id for the current request and return a reset token."""
    return _mcp_session_id.set(value.strip() if isinstance(value, str) and value.strip() else None)


def reset_mcp_session_id(token) -> None:
    _mcp_session_id.reset(token)


def get_mcp_session_id() -> Optional[str]:
    return _mcp_session_id.get()


def set_mcp_request_id(value: Optional[str]):
    """Set the transport request id for logs and return a reset token."""
    return _mcp_request_id.set(value.strip() if isinstance(value, str) and value.strip() else None)


def reset_mcp_request_id(token) -> None:
    _mcp_request_id.reset(token)


def get_mcp_request_id() -> Optional[str]:
    return _mcp_request_id.get()
