"""FastAPI app for the tweak server.

Routes:

    GET  /                                       health
    GET  /projects/{project_id}/tweak            form HTML
    GET  /api/projects/{project_id}              current props + schemas (JSON)
    POST /api/projects/{project_id}/tweak        submit tweak → enqueue async MCP render (JSON 202)
    GET  /api/projects/{project_id}/jobs         list jobs for a project (JSON)

Run:
    uvicorn tweak_server.app:app --port 8901 --host 127.0.0.1

Env vars (all optional):
    MCP_HTTP_URL           default http://127.0.0.1:8900
    MCP_API_TOKEN          Bearer token for MCP (if configured there)
    TWEAK_SERVER_BEARER    token clients must send in X-Tweak-Token (empty = off)
    TWEAK_RENDER_TIMEOUT_S default 600

See: docs/plans/rosy-dazzling-bear.md
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import jobs as jobs_module
from . import queue
from .auth import TWEAK_SERVER_BEARER, require_token
from .mcp_client import MCPError, get_client, shutdown_client, startup_client
from .props_schema import (
    TweakPayload,
    VALID_THEMES,
    merge_into_template,
)

_log = logging.getLogger("tweak_server.app")

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Baked-in fallback template (matches remotion-composer/public/sample-props/
# the-refactor-serenade-sample.json shape). Used when a project has no
# pre-existing props file.
DEFAULT_TEMPLATE_NAME = "default_tweak_template"
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "remotion-composer" / "public" / "sample-props" / "the-refactor-serenade-sample.json"


# -----------------------------------------------------------------------------
# Logging / startup
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("TWEAK_SERVER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("Starting tweak server...")
    if not TWEAK_SERVER_BEARER:
        _log.warning(
            "TWEAK_SERVER_BEARER is empty — tweak endpoints accept any caller. "
            "Set the env var before exposing publicly."
        )
    try:
        await startup_client()
    except MCPError as e:
        _log.error("MCP client failed to initialize: %s", e)
        # Don't crash — let health endpoint still respond so caller can debug
    yield
    _log.info("Shutting down tweak server...")
    await shutdown_client()


app = FastAPI(
    title="OpenMontage Tweak Server",
    version="0.1.0",
    description="Sidecar MCP client for end-user render-script micro-tweaks.",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# -----------------------------------------------------------------------------
# Render artifact serving (so the form can preview the freshly-rendered mp4)
# -----------------------------------------------------------------------------

@app.get("/renders/{project_id}/{filename}")
async def serve_render(project_id: str, filename: str) -> FileResponse:
    """Serve a rendered mp4 from projects/<id>/renders/ for preview-in-page."""
    from fastapi.responses import FileResponse as _FR

    # Defensive path validation — never let users reach arbitrary files
    if "/" in filename or ".." in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="only .mp4 files are served")

    project_dir = PROJECTS_DIR / project_id
    renders_dir = project_dir / "renders"
    target = (renders_dir / filename).resolve()
    if not str(target).startswith(str(renders_dir.resolve())):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="render not found")
    return _FR(target, media_type="video/mp4")


# -----------------------------------------------------------------------------
# Template discovery
# -----------------------------------------------------------------------------


def _load_default_template() -> dict[str, Any]:
    """Load the baked-in sample props file as the tweak starting point."""
    if not DEFAULT_TEMPLATE_PATH.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"Default template missing at {DEFAULT_TEMPLATE_PATH}",
        )
    with open(DEFAULT_TEMPLATE_PATH) as f:
        return json.load(f)


def _load_project_template(project_id: str) -> dict[str, Any] | None:
    """Look for a project-specific props file (future hook). For now returns None."""
    candidate = PROJECTS_DIR / project_id / "remotion_props.json"
    if candidate.is_file():
        try:
            with open(candidate) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Could not load %s: %s", candidate, exc)
    return None


def get_template(project_id: str) -> dict[str, Any]:
    """Project-specific template if present, else default."""
    return _load_project_template(project_id) or _load_default_template()


# -----------------------------------------------------------------------------
# Decision-log append (append-only, per CLAUDE.md §invariant 6)
# -----------------------------------------------------------------------------

def _append_decision_log(project_id: str, entry: dict[str, Any]) -> Path:
    """Append a tweak entry to projects/<id>/decision_log_tweak_<revN>.json.

    Does NOT mutate the existing decision_log.json — pure append per the
    append-only contract (CLAUDE.md invariant 6, rosydazzlingbear §5).
    """
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"project {project_id!r} not found on disk"
        )
    existing = list(project_dir.glob("decision_log_tweak_rev*.json"))
    next_rev = len(existing) + 1
    path = project_dir / f"decision_log_tweak_rev{next_rev:03d}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    return path


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/")
async def health() -> dict[str, Any]:
    """Liveness probe + capability snapshot."""
    return {
        "service": "tweak_server",
        "version": app.version,
        "mcp_initialized": get_client()._initialized,
        "auth_required": bool(TWEAK_SERVER_BEARER),
        "themes": VALID_THEMES,
    }


@app.get("/projects/{project_id}/tweak", response_class=HTMLResponse)
async def tweak_form(project_id: str) -> HTMLResponse:
    """Serve the tweak HTML form. No auth on the HTML itself; the JS sends
    X-Tweak-Token on POST."""
    html_path = STATIC_DIR / "tweak.html"
    if not html_path.is_file():
        raise HTTPException(status_code=500, detail="tweak.html not found")
    html = html_path.read_text(encoding="utf-8")
    # Inject project_id as a JS global so tweak.js can use it
    html = html.replace(
        "</head>",
        f"<script>window.__PROJECT_ID__ = {json.dumps(project_id)};</script></head>",
        1,
    )
    return HTMLResponse(html)


@app.get("/api/projects/{project_id}")
async def get_project_props(
    project_id: str,
    _auth: None = Depends(require_token),
) -> dict[str, Any]:
    """Return the current props template + shape metadata for the form to render."""
    template = get_template(project_id)
    project_dir = PROJECTS_DIR / project_id
    return {
        "project_id": project_id,
        "project_exists_on_disk": project_dir.is_dir(),
        "template": template,
        "themes": VALID_THEMES,
        "animations": ["zoom-in", "pan-down", "ken-burns", "none"],
        "field_ranges": {
            "fontSize": [24, 200],
            "audio_volume": [0.0, 1.0],
            "audio_fade": [0.0, 3.0],
            "audio_offset": [0.0, 30.0],
        },
    }


@app.post("/api/projects/{project_id}/tweak")
async def submit_tweak(
    project_id: str,
    raw: dict[str, Any],
    _auth: None = Depends(require_token),
) -> JSONResponse:
    """Validate tweak, merge into template, enqueue MCP render, log decision.

    Returns HTTP 202 with ``{job_id, status: "queued", ...}`` immediately;
    the actual render runs in the background via ``queue.submit_render_job``.
    """
    # 1) Validate payload shape
    try:
        tweak = TweakPayload.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "validation": json.loads(exc.json())},
        )

    # 2) Load template + merge
    template = get_template(project_id)
    try:
        full_props = merge_into_template(template, tweak)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "merge_failed", "message": str(exc)},
        )

    # 3) Pick output path inside project's renders/ dir
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"project {project_id!r} not found on disk; nothing to render into",
        )
    renders_dir = project_dir / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # 4) Generate job_id + staging_id (deterministic link between Job and MCP render)
    job_id = uuid.uuid4().hex[:12]
    staging_id = f"tweak-{job_id}"
    output_path = str(renders_dir / f"tweak-{timestamp}.mp4")

    # 5) Pre-create the Job so list endpoint sees it immediately
    jobs_module.get_store().create(project_id, job_id=job_id)

    # 6) Decision-log entry (must be appended BEFORE the render — survives crash)
    log_entry = {
        "category": "user_tweak",
        "subject": "tweak_form_submission",
        "actor": raw.get("_actor", "anonymous"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "job_id": job_id,
        "staging_id": staging_id,
        "output_path": output_path,
        "diff_summary": {
            "theme": tweak.theme,
            "cuts_touched": [c.id for c in tweak.cuts],
            "audio_blocks_touched": (
                [k for k in ("narration", "music")
                 if getattr(tweak.audio, k, None) is not None]
                if tweak.audio else []
            ),
        },
        "comment": tweak.comment,
        "full_props_kept_at": f"decision_log_tweak_rev_snapshot_{staging_id}.json",
    }
    # Save a snapshot of the merged full props alongside the log entry so we
    # can replay the exact render later (cheap insurance — file is small).
    snapshot_path = project_dir / f"decision_log_tweak_rev_snapshot_{staging_id}.json"
    try:
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(full_props, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        _log.warning("could not write props snapshot %s: %s", snapshot_path, exc)
        log_entry["snapshot_error"] = str(exc)

    try:
        log_path = _append_decision_log(project_id, log_entry)
        log_entry["decision_log_path"] = str(log_path.relative_to(project_dir))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("decision_log write failed")
        # Mark the job as failed so the caller polling /jobs sees the truth
        jobs_module.get_store().update(
            job_id, status="failed", phase="failed",
            error=f"decision_log_write_failed: {exc}",
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "decision_log_write_failed", "message": str(exc)},
        )

    # 7) Dispatch MCP render as a background asyncio task (non-blocking).
    #    The HTTP handler returns 202 immediately; the render runs on the loop.
    await queue.submit_render_job(
        job_id=job_id,
        project_id=project_id,
        edit_decisions=full_props,
        output_path=output_path,
        staging_id=staging_id,
    )

    # 8) Return the new async-job contract: {job_id, status, decision_log, ...}
    payload = {
        "job_id": job_id,
        "status": "queued",
        "project_id": project_id,
        "staging_id": staging_id,
        "decision_log": log_entry.get("decision_log_path"),
        "comment": tweak.comment,
        "merged_cuts_touched": [c.id for c in tweak.cuts],
    }
    return JSONResponse(payload, status_code=202)


# ---- async job endpoints (Feature B) ----

@app.get("/api/projects/{project_id}/jobs")
async def list_project_jobs(
    project_id: str,
    limit: int = 50,
    _auth: None = Depends(require_token),
) -> dict[str, Any]:
    """List async render jobs for a project, newest first."""
    return {
        "project_id": project_id,
        "jobs": [j.to_dict() for j in queue.list_jobs(project_id, limit)],
    }


# -----------------------------------------------------------------------------
# Progress endpoints (Feature A — SSE bridge to MCP)
# -----------------------------------------------------------------------------
# These endpoints are read-only views into the job store + an SSE pipe to
# the MCP server. They never mutate Job state. Feature B will populate the
# store when it refactors ``submit_tweak`` to run async.
from .progress import router as progress_router

app.include_router(progress_router)


# -----------------------------------------------------------------------------
# Local debug (not used in production)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TWEAK_SERVER_PORT", "8901"))
    host = os.environ.get("TWEAK_SERVER_HOST", "127.0.0.1")
    uvicorn.run(
        "tweak_server.app:app",
        host=host,
        port=port,
        reload=False,
        log_level=os.environ.get("TWEAK_SERVER_LOG_LEVEL", "info").lower(),
    )