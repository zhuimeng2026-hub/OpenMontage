"""Unit tests for ``BearerTokenAuthMiddleware`` X-VClaw-User-Id handling.

Phase 3 of the user-isolation plan (``docs/user-isolation-via-mcp-session.md``).

These tests exercise the middleware directly against a capturing inner ASGI
app, without spinning up the full FastMCP server. They verify that:

  * a valid ``X-VClaw-User-Id`` plus a valid ``MCP_API_TOKEN`` causes the
    ``current_user_id()`` ContextVar to read the sanitized user id *inside*
    the inner app, and is reset to ``None`` after the middleware returns;
  * a missing header leaves ``current_user_id()`` as ``None``;
  * an invalid token never reaches the inner app and never sets the
    ContextVar (security boundary — an attacker cannot forge a user id
    by combining a bogus header with a wrong token, because the header
    is only consulted after the token check);
  * malformed values (CRLF, slash, too long, empty) are silently dropped
    with a warning, not raised — so a bad header can never block an
    otherwise-valid request.

``mcp_server`` is imported on Windows only because
``lib.workbuddy_session`` does ``import fcntl`` at module load. The import
succeeds with an empty stub because fcntl is not *called* until runtime,
and these tests never invoke any code path that touches it.
"""

from __future__ import annotations

import os
import sys
import types
import hashlib
import hmac
import time
import uuid

# Set BEFORE pytest's conftest fixtures are evaluated so this module's tests
# don't pull voicebox into the session. tests/integration/conftest.py has an
# autouse session-scope ``shared_clone_profile`` that probes voicebox
# /health; this module never uses voicebox (it tests a pure ASGI middleware)
# so we tell the conftest fixture to short-circuit.
os.environ.setdefault("MCP_TEST_SKIP_VOICEBOX_FIXTURES", "1")
os.environ.setdefault("OPENMONTAGE_VCLAW_ASSERTION_SECRET", "integration-assertion-secret")

import pytest

# Workaround for Windows: lib.workbuddy_session does ``import fcntl`` at
# module load, which raises ModuleNotFoundError on Windows. The middleware
# never calls into workbuddy_session, so a stub is enough to let the import
# graph resolve. Insert before importing mcp_server.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import mcp_server  # noqa: E402  (sys.modules stub above must come first)


# ---------------------------------------------------------------------------
# Inner ASGI app — captures the value of current_user_id() at the moment
# the middleware invokes it.
# ---------------------------------------------------------------------------


class _CapturingApp:
    """Minimal ASGI app. Records what ``current_user_id()`` returned *during*
    the request so the test can assert the ContextVar was set correctly.

    Also drains ``receive`` so the middleware's body re-dispatch (which
    wraps ``receive`` for ``POST /mcp``) doesn't get stuck waiting for
    more body chunks. Sends a minimal 200 response back through ``send``.
    """

    def __init__(self) -> None:
        self.invoked = False
        self.captured_user_id: object = None

    async def __call__(self, scope, receive, send) -> None:
        self.invoked = True
        self.captured_user_id = mcp_server.current_user_id()
        # Drain the receive iterator. For GET requests no body is expected,
        # but we still call once to consume any "more_body=False" message.
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                break
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


class _SendCollector:
    """Collects http.response.start messages so tests can assert on status."""

    def __init__(self) -> None:
        self.starts: list[dict] = []

    async def __call__(self, message: dict) -> None:
        if message.get("type") == "http.response.start":
            self.starts.append(message)


def _make_scope(headers: list[tuple[bytes, bytes]], path: str = "/mcp") -> dict:
    header_map = dict(headers)
    uid = header_map.get(b"x-vclaw-user-id")
    if uid and b"x-vclaw-user-assertion" not in header_map:
        try:
            user = uid.decode("ascii")
            stamp = int(time.time())
            nonce = uuid.uuid4().hex
            canonical = "\n".join(("v1", user, str(stamp), nonce, "GET", path, "", hashlib.sha256(b"").hexdigest()))
            sig = hmac.new(os.environ["OPENMONTAGE_VCLAW_ASSERTION_SECRET"].encode(), canonical.encode(), hashlib.sha256).hexdigest()
            headers = list(headers) + [(b"x-vclaw-user-assertion", f"v1.{stamp}.{nonce}.{sig}".encode())]
        except (UnicodeDecodeError, KeyError):
            pass
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
    }


async def _noop_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _run_middleware(
    mw: mcp_server.BearerTokenAuthMiddleware,
    scope: dict,
) -> tuple[_CapturingApp, _SendCollector]:
    """Install a fresh capturing app on the middleware and invoke it once."""
    inner = _CapturingApp()
    mw.app = inner
    send = _SendCollector()
    await mw(scope, _noop_receive, send)
    return inner, send


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_id_header_attached_when_token_valid() -> None:
    """Valid header + valid token → inner app sees the sanitized user id,
    and ContextVar is reset after the middleware returns."""
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    inner, send = await _run_middleware(mw, scope)

    assert inner.invoked, "inner app should run when token is valid"
    assert inner.captured_user_id == "alice_42"
    assert send.starts[0]["status"] == 200
    # Cleanup must restore the default — otherwise a leaked ContextVar would
    # leak the previous request's user id into the next request.
    assert mcp_server.current_user_id() is None


@pytest.mark.asyncio
async def test_shared_bearer_cannot_forge_user_header() -> None:
    """A direct MCP bearer caller must not claim a vclaw user identity."""
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = {
        **_make_scope([
            (b"authorization", b"Bearer secret-token"),
            (b"x-vclaw-user-id", b"alice_42"),
        ]),
        "headers": [
            (k, v) for k, v in _make_scope([
                (b"authorization", b"Bearer secret-token"),
                (b"x-vclaw-user-id", b"alice_42"),
            ])["headers"] if k != b"x-vclaw-user-assertion"
        ],
    }
    inner, send = await _run_middleware(mw, scope)
    assert not inner.invoked
    assert send.starts[0]["status"] == 401


@pytest.mark.asyncio
async def test_user_assertion_nonce_cannot_be_replayed() -> None:
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    first, first_send = await _run_middleware(mw, scope)
    second, second_send = await _run_middleware(mw, scope)
    assert first.invoked and first_send.starts[0]["status"] == 200
    assert not second.invoked and second_send.starts[0]["status"] == 401


@pytest.mark.asyncio
async def test_user_assertion_expired_or_secret_missing_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    # Replace the otherwise valid assertion with a stale timestamp while
    # retaining the signed shape; verifier must reject before app dispatch.
    headers = [(k, v) for k, v in scope["headers"] if k != b"x-vclaw-user-assertion"]
    headers.append((b"x-vclaw-user-assertion", b"v1.1.stale_nonce_123456.00" + b"0" * 62))
    scope["headers"] = headers
    inner, send = await _run_middleware(mw, scope)
    assert not inner.invoked and send.starts[0]["status"] == 401

    valid_scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    monkeypatch.delenv("OPENMONTAGE_VCLAW_ASSERTION_SECRET", raising=False)
    inner, send = await _run_middleware(mw, valid_scope)
    assert not inner.invoked and send.starts[0]["status"] == 401


@pytest.mark.asyncio
async def test_malformed_session_header_rejected() -> None:
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"mcp-session-id", b"\xff"),
    ])
    inner, send = await _run_middleware(mw, scope)
    assert not inner.invoked
    assert send.starts[0]["status"] == 400


@pytest.mark.asyncio
async def test_duplicate_identity_header_rejected() -> None:
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", b"alice_42"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    inner, send = await _run_middleware(mw, scope)
    assert not inner.invoked
    assert send.starts[0]["status"] == 400


@pytest.mark.asyncio
async def test_existing_user_session_requires_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lib.principal_registry import Principal

    monkeypatch.setattr(
        mcp_server._principal_registry,
        "lookup",
        lambda _sid: Principal(kind="user", principal_id="alice_42"),
    )
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"mcp-session-id", b"session-alice"),
    ])
    inner, send = await _run_middleware(mw, scope)
    assert not inner.invoked
    assert send.starts[0]["status"] == 401


@pytest.mark.asyncio
async def test_user_id_header_absent_no_crash() -> None:
    """Missing X-VClaw-User-Id is allowed (service-token path). Inner app
    runs and sees ``None`` from the ContextVar."""
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([(b"authorization", b"Bearer secret-token")])
    inner, send = await _run_middleware(mw, scope)

    assert inner.invoked
    assert inner.captured_user_id is None
    assert send.starts[0]["status"] == 200


# ---------------------------------------------------------------------------
# Security boundary: header MUST NOT be honored without a valid token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_id_rejected_invalid_token() -> None:
    """Wrong token must yield 401, must not invoke the inner app, and
    crucially must NOT set the user-id ContextVar — the security contract
    is that user attribution can only happen *after* MCP_API_TOKEN passes.
    A header combined with a bad token is no better than a bad token alone.
    """
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer wrong-token"),
        (b"x-vclaw-user-id", b"alice_42"),
    ])
    inner, send = await _run_middleware(mw, scope)

    assert not inner.invoked, "unauthorized request must not reach inner app"
    assert send.starts, "middleware should have produced a response"
    assert send.starts[0]["status"] == 401
    assert mcp_server.current_user_id() is None


@pytest.mark.asyncio
async def test_user_id_rejected_missing_token() -> None:
    """No Authorization → 401, ContextVar not set. Belt-and-suspenders for
    the same security boundary in case the header is sent alone."""
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([(b"x-vclaw-user-id", b"alice_42")])
    inner, send = await _run_middleware(mw, scope)

    assert not inner.invoked
    assert send.starts[0]["status"] == 401
    assert mcp_server.current_user_id() is None


# ---------------------------------------------------------------------------
# Sanitization: malformed values must be silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, bad_value",
    [
        ("crlf_injection", b"abc\r\ndef"),      # CRLF — would split HTTP headers if reflected
        ("forward_slash", b"../etc/passwd"),   # path traversal char
        ("backslash", b"foo\\bar"),             # backslash — not in allow-list
        ("space", b"foo bar"),                  # space — not in allow-list
        ("too_long_200", b"a" * 200),          # length cap is 128
        ("too_long_129", b"a" * 129),          # one past the cap
        ("empty", b""),                        # absent-equivalent
        ("whitespace_only", b"   "),           # strip → empty
        ("non_ascii_utf8", "café".encode("utf-8")),  # not ascii-decodable
    ],
)
@pytest.mark.asyncio
async def test_user_id_sanitization_rejects_invalid(label: str, bad_value: bytes) -> None:
    """A malformed header value must NOT block the request (inner app runs,
    status 200), and must NOT set the ContextVar to the bad value.

    Each parametrize case is labeled so failures point at the exact pattern
    that broke (CRLF vs slash vs length, etc.).
    """
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", bad_value),
    ])
    inner, send = await _run_middleware(mw, scope)

    if label == "empty":
        assert inner.invoked and send.starts[0]["status"] == 200
    else:
        assert not inner.invoked, f"case {label!r}: malformed identity must fail closed"
        assert send.starts[0]["status"] == 401
    assert mcp_server.current_user_id() is None


# ---------------------------------------------------------------------------
# Sanitization positive cases (boundary checks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ok_value",
    [
        b"alice_42",                      # typical shape from vclaw's newID()
        b"a" * 128,                       # exact upper boundary
        b"X-VClaw-User-Id.is.OK",         # dots + dashes + underscore all allowed
        b"0123456789",                    # digits only
    ],
)
@pytest.mark.asyncio
async def test_user_id_sanitization_accepts_valid(ok_value: bytes) -> None:
    mw = mcp_server.BearerTokenAuthMiddleware(app=None, token="secret-token")
    scope = _make_scope([
        (b"authorization", b"Bearer secret-token"),
        (b"x-vclaw-user-id", ok_value),
    ])
    inner, send = await _run_middleware(mw, scope)

    assert inner.invoked
    assert inner.captured_user_id == ok_value.decode("ascii")
    assert send.starts[0]["status"] == 200


# ---------------------------------------------------------------------------
# Direct unit tests for _sanitize_vclaw_user_id (no ASGI plumbing)
# ---------------------------------------------------------------------------


def test_sanitize_returns_none_for_empty_bytes() -> None:
    assert mcp_server._sanitize_vclaw_user_id(b"") is None


def test_sanitize_rejects_surrounding_whitespace() -> None:
    # The allow-list rejects internal whitespace, but leading/trailing
    # whitespace must be stripped before checking the charset.
    assert mcp_server._sanitize_vclaw_user_id(b"  alice_42  ") is None


def test_sanitize_does_not_raise_on_garbage() -> None:
    """Whatever the bytes look like, the sanitizer must not raise."""
    assert mcp_server._sanitize_vclaw_user_id(b"\x00\x01\x02") is None
    assert mcp_server._sanitize_vclaw_user_id(b"\xff\xfe") is None
