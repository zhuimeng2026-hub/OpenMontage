"""Minimal JSON-RPC client for the OpenMontage MCP server.

Targets the local MCP server at ``MCP_HTTP_URL`` (default ``http://127.0.0.1:8900``)
using the streamable-http transport (POST to ``/mcp``). Bearer token is loaded
from ``MCP_API_TOKEN`` env var when present.

What we use it for:
    1. ``initialize`` once at startup (per-process).
    2. ``tools/call`` with ``execute_tool`` proxying to ``video_compose`` with
       ``operation="remotion_render"`` (synchronous wait for MVP — see plan §3.2).

We intentionally do NOT use ``create_remotion_video_share`` — that high-level
workflow requires the full ``asset_manifest`` + ``proposal_packet`` +
``scene_plan`` triplet, which is overkill for "tweak one video" UX. Direct
``remotion_render`` lets users iterate on existing project state without
re-running the pipeline.

See: docs/plans/rosy-dazzling-bear.md §3.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

_log = logging.getLogger("tweak_server.mcp_client")

MCP_HTTP_URL = os.environ.get("MCP_HTTP_URL", "http://127.0.0.1:8900")
MCP_API_TOKEN = os.environ.get("MCP_API_TOKEN", "")
# Render timeout: 10 minutes for a single tweak render
TWEAK_RENDER_TIMEOUT_S = float(os.environ.get("TWEAK_RENDER_TIMEOUT_S", "600"))


class MCPError(RuntimeError):
    """Raised when MCP returns a JSON-RPC error or HTTP failure."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class MCPClient:
    """Async JSON-RPC client. One per process; cheap to share."""

    def __init__(self, base_url: str = MCP_HTTP_URL, api_token: str = MCP_API_TOKEN):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._initialized = False
        self._client: httpx.AsyncClient | None = None
        # MCP streamable-http is stateful: server returns `mcp-session-id`
        # in the initialize response and every subsequent request must echo
        # it. Without it, server returns 400 "Mcp-Session-Id is required".
        self._session_id: str | None = None

    async def startup(self) -> None:
        """Open HTTP connection + run MCP ``initialize``. Idempotent."""
        if self._initialized:
            return
        headers = {
            "Content-Type": "application/json",
            # MCP streamable-http spec: client must accept both JSON and
            # event-stream. Without text/event-stream MCP returns
            # "Not Acceptable: Client must accept application/json".
            "Accept": "application/json, text/event-stream",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=TWEAK_RENDER_TIMEOUT_S, write=30.0, pool=10.0),
        )
        try:
            await self._post(
                method="initialize",
                params={
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "openmontage-tweak-server", "version": "0.1.0"},
                },
                request_id=1,
            )
        except MCPError as e:
            await self._client.aclose()
            self._client = None
            raise
        self._initialized = True
        _log.info(
            "MCP client initialized: base_url=%s session_id=%s",
            self.base_url,
            self._session_id or "(none)",
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._initialized = False

    async def render_remotion(
        self,
        *,
        edit_decisions: dict[str, Any],
        output_path: str,
        staging_id: str,
        remotion_timeout_ms: int = 600_000,
    ) -> dict[str, Any]:
        """Call ``video_compose(operation='remotion_render', ...)`` via execute_tool.

        Returns the unwrapped ``ExecuteResult`` dict with ``success``/``data``/
        ``artifacts``/``error``/``cost_usd`` keys. Raises MCPError on transport
        or RPC failures.
        """
        if not self._initialized:
            await self.startup()

        inputs = {
            "operation": "remotion_render",
            "edit_decisions": edit_decisions,
            "output_path": output_path,
            "staging_id": staging_id,
            "remotion_timeout_ms": remotion_timeout_ms,
        }
        started = time.monotonic()
        # MCP streamable-http tool responses are wrapped as:
        #   { "content": [{"type": "text", "text": "<JSON>"}], "isError": bool }
        # We unwrap once and parse the inner JSON as ExecuteResult.
        envelope = await self._post(
            method="tools/call",
            params={"name": "execute_tool", "arguments": {
                "tool_name": "video_compose",
                "inputs": inputs,           # NOTE: execute_tool uses `inputs`, not `arguments`
            }},
            request_id=int(time.time() * 1000) % 1_000_000,
        )
        _log.info(
            "remotion_render call took %.1fs; output_path=%s",
            time.monotonic() - started,
            output_path,
        )
        return self._unwrap(envelope)

    @staticmethod
    def _unwrap(envelope: Any) -> dict[str, Any]:
        """Unwrap an MCP tool response envelope to its inner result dict.

        MCP streamable-http wraps every tool response as
            { "content": [{"type":"text","text": "<json>"}], "isError": bool }
        Some tools (notably ``initialize``) return their result unwrapped — we
        pass those through. If the text payload is itself a JSON object we
        return it; otherwise we return a minimal dict carrying isError.
        """
        if not isinstance(envelope, dict):
            return {"success": False, "error": f"unexpected envelope: {envelope!r}"}
        # Unwrapped (e.g. initialize result) → return as-is
        if "content" not in envelope:
            return envelope
        is_error = bool(envelope.get("isError"))
        content = envelope.get("content") or []
        if not content:
            return {"success": False, "error": "empty content in MCP response"}
        first = content[0] if isinstance(content, list) else content
        text = first.get("text") if isinstance(first, dict) else None
        if text is None:
            return {"success": False, "error": "no text in MCP content", "is_error": is_error}
        try:
            inner = json.loads(text)
        except json.JSONDecodeError:
            return {
                "success": not is_error,
                "error": None if not is_error else text[:500],
                "raw_text": text[:500],
                "is_error": is_error,
            }
        if isinstance(inner, dict):
            inner.setdefault("is_error", is_error)
            return inner
        return {"success": not is_error, "data": inner, "is_error": is_error}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Debug helper — list registered MCP tools."""
        if not self._initialized:
            await self.startup()
        result = await self._post(
            method="tools/list",
            params={},
            request_id=int(time.time() * 1000) % 1_000_000,
        )
        return result.get("tools", []) if isinstance(result, dict) else []

    async def _post(self, *, method: str, params: dict[str, Any], request_id: int) -> Any:
        assert self._client is not None, "call startup() first"
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        # Echo the session id on every call after initialize. httpx's `headers`
        # merge appends — passing `mcp-session-id` here overrides anything else.
        extra_headers: dict[str, str] = {}
        if self._session_id:
            extra_headers["mcp-session-id"] = self._session_id
        try:
            resp = await self._client.post("/mcp", json=body, headers=extra_headers)
        except httpx.HTTPError as exc:
            raise MCPError(f"HTTP transport error: {exc}") from exc
        # First successful initialize response carries the session id; capture
        # it once and reuse for the lifetime of this client.
        if self._session_id is None:
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
        if resp.status_code == 401:
            raise MCPError(
                f"MCP server rejected Bearer token (HTTP 401). "
                f"Check MCP_API_TOKEN. Body: {resp.text[:300]}"
            )
        if resp.status_code == 406 and method != "initialize":
            # 406 here usually means missing Accept header variant; we already
            # send application/json. Log body for debugging.
            raise MCPError(
                f"MCP HTTP 406 (Not Acceptable): {resp.text[:500]}"
            )
        if resp.status_code >= 400:
            raise MCPError(
                f"MCP server returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise MCPError(f"MCP returned non-JSON: {resp.text[:500]}") from exc

        if "error" in data:
            err = data["error"]
            raise MCPError(
                f"MCP JSON-RPC error: {err.get('message', '?')} "
                f"(code={err.get('code', '?')})",
                code=err.get("code"),
                data=err.get("data"),
            )

        return data.get("result")


# Module-level singleton (set by app.py at startup)
_client: MCPClient | None = None


def get_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


async def startup_client() -> None:
    """Called from FastAPI lifespan — initializes the shared client."""
    client = get_client()
    await client.startup()


async def shutdown_client() -> None:
    global _client
    if _client is not None:
        await _client.shutdown()
        _client = None