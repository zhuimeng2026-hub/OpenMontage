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
    """Stream Server-Sent Events for ``job_id``.

    For jobs owned by the tweak server (the common case after Feature B),
    we emit a local heartbeat every 1s by polling the Job state. This is the
    authoritative source for tweak-server-created jobs — MCP doesn't know
    about them unless they go through the high-level create_remotion_video_share.

    Best-effort: when MCP happens to have its own SSE stream for the same id
    (e.g. high-level workflow), we forward those bytes raw too — mixed into
    the same stream. Stream closes when the JobStore reports completed/failed.
    """
    import asyncio
    import json as _json

    from .jobs import get_store

    HEARTBEAT_INTERVAL_S = 1.0

    async def event_stream() -> Any:
        last_status = last_phase = None
        last_percent = -1.0
        # Brief initial delay so the Job entry has time to land if the
        # client connected right after the POST returned.
        await asyncio.sleep(0.05)

        mcp_q = asyncio.Queue()
        mcp_started = False

        async def _pump_mcp():
            nonlocal mcp_started
            url = f"{MCP_HTTP_URL}/render-progress/{job_id}"
            headers = {"Accept": "text/event-stream"}
            if MCP_API_TOKEN:
                headers["Authorization"] = f"Bearer {MCP_API_TOKEN}"
            timeout = httpx.Timeout(connect=5.0, read=24 * 60 * 60.0, write=5.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code == 200:
                            mcp_started = True
                            async for chunk in resp.aiter_bytes():
                                await mcp_q.put(("mcp", chunk))
                except httpx.HTTPError:
                    pass
                finally:
                    await mcp_q.put(("mcp", None))  # sentinel

        pump_task = asyncio.create_task(_pump_mcp())

        try:
            for tick in range(7200):  # up to 2 hours
                # Check client disconnect
                disconnected = await request.is_disconnected()
                if disconnected:
                    break

                # Drain any MCP chunks (non-blocking via get_nowait)
                while not mcp_q.empty():
                    try:
                        kind, chunk = mcp_q.get_nowait()
                        if chunk is None:
                            # MCP done; mark so we stop awaiting it
                            mcp_started = False
                            continue
                        yield chunk
                    except asyncio.QueueEmpty:
                        break

                # Local JobStore heartbeat
                job = get_store().get(job_id)
                if job is None:
                    payload = {
                        "job_id": job_id,
                        "phase": "unknown",
                        "status": "unknown",
                        "message": "no such job",
                    }
                    yield (
                        f"event: progress\n"
                        f"data: {_json.dumps(payload)}\n\n"
                    ).encode("utf-8")
                    return

                # Emit on EVERY tick so the browser sees a continuously
                # flowing progress stream (otherwise long stable phases —
                # e.g. 130s of MCP render with no internal updates — would
                # leave the UI frozen and looking dead).
                payload = {
                    "event": "render_progress",
                    "render_job_id": job_id,
                    "phase": job.phase or job.status,
                    "status": job.status,
                    "percent": job.percent,
                    "message": job.message,
                    "staging_id": job.staging_id,
                    "output_path": job.output_path,
                }
                yield (
                    f"event: progress\n"
                    f"data: {_json.dumps(payload, default=str)}\n\n"
                ).encode("utf-8")

                if job.status in ("completed", "failed"):
                    terminal = {
                        "event": "render_progress",
                        "render_job_id": job_id,
                        "phase": job.phase or job.status,
                        "status": job.status,
                        "percent": job.percent,
                        "message": job.message or job.error,
                        "staging_id": job.staging_id,
                        "output_path": job.output_path,
                        "result": job.result,
                        "error": job.error,
                    }
                    yield (
                        f"event: terminal\n"
                        f"data: {_json.dumps(terminal, default=str)}\n\n"
                    ).encode("utf-8")
                    return

                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        finally:
            if not pump_task.done():
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )