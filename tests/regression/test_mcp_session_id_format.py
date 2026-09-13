"""Regression test: OM MCP must emit a valid Mcp-Session-Id on initialize.

Bug (vclaw report 2026-09-06, see
``/opt/vclaw/docs/openmontage-mcp-session-id-bug-2026-09-06.md``):
vclaw observed the OpenMontage MCP at ``/mcp`` occasionally returning the
literal string ``"System.String[]"`` as the ``Mcp-Session-Id`` response
header — a .NET ``String[].ToString()`` sentinel leaking from a path where
a ``string[]`` was assigned where a single ``string`` was expected. The
value then persisted in vclaw's ``mcp_sessions`` table and triggered
misleading ``AUTH_RESOURCE_FORBIDDEN`` errors on reuse.

This OM Python codebase does not currently reproduce the bug (the MCP
library generates the sid as ``uuid4().hex`` — a 32-char lowercase hex
token, see ``.venv/.../mcp/server/streamable_http_manager.py:288``). The
test below locks in that contract on this codebase: if any future change
ever assigns a list/tuple/dict/non-token value to the sid slot, the test
fails here too. vclaw has shipped a strict guard
(``IsValidMCPSessionID``, ``^[0-9a-fA-F]{32}$`` or RFC 4122 dashed UUID)
in commit ``ff571b2``; the regex below mirrors that guard so both sides
fail loud if the formats ever drift apart.

The test is skipped gracefully if the OM MCP at ``:8900`` is not running,
matching the pattern used in ``tests/integration/``.

Run with::

    .venv/bin/python -m pytest tests/regression/test_mcp_session_id_format.py -v
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Mirror vclaw's IsValidMCPSessionID regex (commit ff571b2, 2026-09-06).
# If vclaw relaxes this, update both sides in lockstep — see the
# vclaw-side fix plan in the linked bug report's "vclaw-side Mitigation"
# section.
SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{32}$"
    r"|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

OM_DEFAULT_URL = "http://127.0.0.1:8900/mcp"
OM_DEFAULT_HOST = "127.0.0.1"
OM_DEFAULT_PORT = 8900
PROBE_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_bearer_token() -> str | None:
    """Pull the MCP bearer token from the env or the repo .env file."""
    for env in ("OM_MCP_TOKEN", "MCP_API_TOKEN"):
        val = os.environ.get(env)
        if val:
            return val.strip()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("MCP_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _probe_initialize(url: str, token: str) -> tuple[int, dict[str, str], str]:
    """Send a single initialize request, return ``(status, headers, body)``."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-session-id-regression", "version": "0.0.1"},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry headers
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — defensive
            pass
        return exc.code, dict(exc.headers or {}), body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpSessionIdFormat(unittest.TestCase):
    """Probe the live OM MCP at :8900/mcp and assert ``Mcp-Session-Id`` is a
    well-formed opaque token. See module docstring for the upstream bug
    and the coordination note with vclaw."""

    URL = OM_DEFAULT_URL
    HOST = OM_DEFAULT_HOST
    PORT = OM_DEFAULT_PORT

    @classmethod
    def setUpClass(cls) -> None:
        if not _port_open(cls.HOST, cls.PORT):
            raise unittest.SkipTest(f"OM MCP not reachable at {cls.HOST}:{cls.PORT}")
        token = _read_bearer_token()
        if not token:
            raise unittest.SkipTest(
                "MCP_API_TOKEN (or OM_MCP_TOKEN) not set in env or .env"
            )
        cls._status, cls._headers, cls._body = _probe_initialize(cls.URL, token)

    def _sid(self) -> str | None:
        # The MCP library uses lowercase ``mcp-session-id``; vclaw clients
        # tolerate either case. Look up both so a future rename in the MCP
        # library still produces a clear failure here.
        for key in ("mcp-session-id", "Mcp-Session-Id"):
            val = self._headers.get(key)
            if val:
                return val
        return None

    def test_initialize_returns_2xx(self) -> None:
        self.assertEqual(
            self._status, 200,
            f"initialize returned HTTP {self._status}; body={self._body[:200]!r}",
        )

    def test_session_id_header_present(self) -> None:
        sid = self._sid()
        self.assertIsNotNone(
            sid,
            f"no Mcp-Session-Id header in response headers: {sorted(self._headers.keys())}",
        )
        self.assertTrue(
            sid.strip(),
            "Mcp-Session-Id header present but empty",
        )

    def test_session_id_matches_vclaw_contract(self) -> None:
        """Guard the exact bug from vclaw's 2026-09-06 report.

        vclaw's ``IsValidMCPSessionID`` (in ``store_mcp_sessions.go``)
        accepts 32 hex chars or RFC 4122 dashed UUID — anything else is
        rejected with ``ErrInvalidSessionID``. This test mirrors that
        contract on the OM side: if OM ever emits a value outside this
        shape (e.g. the literal ``"System.String[]"`` from a stray
        ``string[]`` assignment), the test fails loud and points to the
        vclaw bug report for context.
        """
        sid = self._sid()
        self.assertIsNotNone(sid, "no Mcp-Session-Id header (see other test)")
        self.assertRegex(
            sid,
            SESSION_ID_PATTERN.pattern,
            msg=(
                f"Mcp-Session-Id={sid!r} does not match the vclaw-validated "
                f"contract ({SESSION_ID_PATTERN.pattern}). This is the exact "
                f"bug vclaw reported on 2026-09-06 — see "
                f"/opt/vclaw/docs/openmontage-mcp-session-id-bug-2026-09-06.md. "
                f"Check the OM code path that constructs the initialize "
                f"response and assigns to the Mcp-Session-Id header."
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
