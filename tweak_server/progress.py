"""SSE progress bridge — read-only job state + SSE pipe from MCP.

Two endpoints:

    GET /api/projects/{project_id}/jobs/{job_id}
        → JSON snapshot of the current Job from ``JobStore`` (404 if unknown).
    GET /api/projects/{project_id}/jobs/{job_id}/events
        → Server-Sent Events streamed straight from
          ``GET http://127.0.0.1:8900/render-progress/{job_id}`` on the MCP
          server. We pipe bytes verbatim — no parsing, no transformation —
          so the SSE semantics the MCP server emits survive the hop.

This module is **read-only**: it never mutates Job state. Feature B owns the
mutation surface (status transitions, error capture, output_path finalize).

Auth: ``Depends(require_token)`` — same ``X-Tweak-Token`` as the rest of the
tweak API. SSE clients (browsers using ``EventSource``) cannot set custom
headers, so the token may also travel as a ``?token=`` query parameter on
the SSE endpoint (browser-only escape hatch — fetch() still uses the
header). The status endpoint stays header-only (``require_token``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import _check_token, require_token  # internal helper, see below
from .jobs import Job, get_store

_log = logging.getLogger("tweak_server.progress")

# Same defaults as mcp_client.py — duplicated to avoid coupling (mcp_client
# is for JSON-RPC tool calls, this is for SSE; different transport).
MCP_HTTP_URL = os.environ.get("MCP_HTTP_URL", "http://127.0.0.1:8900").rstrip("/")
MCP_API_TOKEN = os.environ.get("MCP_API_TOKEN", "")

router = APIRouter(prefix="/api/projects", tags=["progress"])


async def _sse_token_auth(
    request: Request,
    token: str | None = Query(default=None, alias="token"),
    x_tweak_token: str | None = None,
) -> None:
    """Auth dep for the SSE endpoint.

    Accepts the token via ``X-Tweak-Token`` header (reverse proxies / native
    fetch) OR ``?token=`` query string (browser ``EventSource`` which can't
    set custom headers). If neither is present, falls back to the same
    check as ``require_token`` so behavior matches for header callers.
    """
    # Header can be read off the request directly since Query(default=None)
    # above doesn't capture it.
    header_token = request.headers.get("x-tweak-token")
    provided = header_token or token
    _check_token(provided)


# -----------------------------------------------------------------------------
# GET /api/projects/{project_id}/jobs/{job_id}
# -----------------------------------------------------------------------------

@router.get("/{project_id}/jobs/{job_id}")
async def get_job_state(
    project_id: str,
    job_id: str,
    _auth: None = Depends(require_token),
) -> JSONResponse:
    """Return the current Job snapshot as JSON.

    Shape (stable contract for the browser):
        {job_id, project_id, status, percent, phase, message,
         staging_id, output_path, result, error, created_at, updated_at}

    Status codes:
        200 — Job exists; payload is the full snapshot.
        404 — Job unknown (typo, expired, never created). Browsers treat
              this as "give up"; UX should show "render not found".
    """
    job: Job | None = get_store().get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "job_not_found", "job_id": job_id},
        )
    payload: dict[str, Any] = job.to_dict()
    # Always echo the ids at the top level too so the browser doesn't have to
    # know the dataclass field names match path params.
    payload.setdefault("job_id", job.id)
    payload.setdefault("project_id", job.project_id)
    return JSONResponse(payload)


# -----------------------------------------------------------------------------
# GET /api/projects/{project_id}/jobs/{job_id}/events  (SSE proxy)
# -----------------------------------------------------------------------------

@router.get("/{project_id}/jobs/{job_id}/events")
async def stream_job_events(
    project_id: str,
    job_id: str,
    request: Request,
    _auth: None = Depends(_sse_token_auth),
) -> StreamingResponse:
    """Stream Server-Sent Events for ``job_id`` from the MCP server.

    We open ``httpx.AsyncClient.stream("GET", <mcp>/render-progress/<id>)``
    and pipe its bytes through a ``StreamingResponse`` with
    ``media_type="text/event-stream"``. **No parsing** — every byte the MCP
    server emits is forwarded as-is so the ``data:``/``event:``/``id:`` lines
    survive intact and the browser's EventSource can interpret them.

    The job_id is owned by the MCP server's progress bus; we do not gate on
    it being in our local JobStore (the local store is updated by Feature B
    based on SSE events, so a brand-new job may be known to MCP before we
    see it). If MCP returns 404 for an unknown job_id the streaming response
    simply closes — the browser's EventSource ``error`` handler fires.
    """
    url = f"{MCP_HTTP_URL}/render-progress/{job_id}"
    headers = {"Accept": "text/event-stream"}
    if MCP_API_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_API_TOKEN}"

    # Connect timeout short (5s) — we don't want to block the response.
    # Read timeout very long (24h) — SSE streams can be open for ages.
    timeout = httpx.Timeout(connect=5.0, read=24 * 60 * 60.0, write=5.0, pool=5.0)

    client = httpx.AsyncClient(timeout=timeout)

    async def relay() -> Any:
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                # If MCP returns 4xx/5xx, forward the status code in a single
                # synthetic SSE event so the browser EventSource can surface
                # the failure (it normally only sees net errors, not HTTP
                # status). For 200 we pipe raw bytes through.
                if resp.status_code != 200:
                    body = await resp.aread()
                    msg = (
                        f"event: error\n"
                        f"data: {{\"status\": {resp.status_code}, "
                        f"\"body\": {body.decode('utf-8', errors='replace')[:500]!r}}}\n\n"
                    )
                    yield msg.encode("utf-8")
                    return
                async for chunk in resp.aiter_bytes():
                    # Honour client disconnect — bail out of the inner loop.
                    if await request.is_disconnected():
                        _log.debug("client disconnected mid-stream job_id=%s", job_id)
                        break
                    yield chunk
        except httpx.HTTPError as exc:
            _log.warning("SSE upstream error job_id=%s: %s", job_id, exc)
            yield (
                f"event: error\n"
                f"data: {{\"status\": 502, \"message\": \"upstream_error\", "
                f"\"detail\": \"{str(exc)[:300]!r}\"}}\n\n"
            ).encode("utf-8")
        finally:
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )