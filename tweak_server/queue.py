"""Async job dispatch — wraps MCP render in asyncio.create_task."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .jobs import Job, get_store
from .mcp_client import MCPError, get_client

_log = logging.getLogger("tweak_server.queue")


async def submit_render_job(
    *,
    job_id: str,
    project_id: str,
    edit_decisions: dict[str, Any],
    output_path: str,
    staging_id: str,
    remotion_timeout_ms: int = 600_000,
) -> Job:
    """Schedule an MCP render as a background task.

    Creates the Job (caller already passed a deterministic job_id) and starts
    an asyncio task that updates the Job's status as it progresses. Returns
    the initial queued Job.
    """
    store = get_store()
    job = store.get(job_id)
    if job is None:
        # Caller didn't pre-create; do it now.
        job = store.create(project_id, job_id=job_id)
    job.status = "queued"
    job.staging_id = staging_id
    job.output_path = output_path
    store.update(job.id, status="queued", staging_id=staging_id, output_path=output_path)

    async def _run():
        try:
            store.update(job.id, status="rendering", phase="starting",
                         percent=0.0, message="calling MCP")
            client = get_client()
            result = await client.render_remotion(
                edit_decisions=edit_decisions,
                output_path=output_path,
                staging_id=staging_id,
                remotion_timeout_ms=remotion_timeout_ms,
            )
            success = bool(result.get("success"))
            if success:
                store.update(
                    job.id,
                    status="completed",
                    phase="completed",
                    percent=100.0,
                    message="render finished",
                    result=result,
                    output_path=result.get("output_path") or output_path,
                    error=None,
                )
            else:
                store.update(
                    job.id,
                    status="failed",
                    phase="failed",
                    error=result.get("error") or "unknown error",
                    result=result,
                )
        except MCPError as exc:
            _log.exception("background render MCPError job=%s", job.id)
            store.update(job.id, status="failed", phase="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            _log.exception("background render crashed job=%s", job.id)
            store.update(job.id, status="failed", phase="failed",
                         error=f"{type(exc).__name__}: {exc}")
        finally:
            _log.info("job %s done; status=%s", job.id,
                      (get_store().get(job.id).status if get_store().get(job.id) else "?"))

    asyncio.create_task(_run(), name=f"render-{job.id}")
    return job


def list_jobs(project_id: str, limit: int = 50) -> list[Job]:
    return get_store().list_for_project(project_id, limit=limit)