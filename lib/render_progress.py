"""Thread-safe publish/subscribe bus for live render progress.

The render pipeline runs in a daemon thread (see ``mcp_server._run_render_job``)
but progress is consumed by an async SSE endpoint on the Starlette event loop.
We bridge the two with a plain ``queue.Queue`` per ``render_job_id``: the worker
thread publishes events (``publish`` / ``progress_event``), the SSE handler drains
them off the event loop via ``asyncio.to_thread`` with a small timeout so it can
emit heartbeats when the pipeline is idle.

The bus is intentionally O(1) per publish and holds no state beyond the live
subscriber lists — finished jobs simply have no subscribers and ``publish`` is a
no-op.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Optional


# render_job_id -> list of subscriber queues
_subs: dict[str, list[queue.Queue]] = {}
_lock = threading.Lock()


def subscribe(job_id: str) -> queue.Queue:
    """Register a subscriber for a render job and return its queue.

    The caller is responsible for calling :func:`unsubscribe` when done so the
    entry does not leak (the bus forgets jobs with no subscribers, but an
    un-drained queue keeps *this* job's list alive).
    """
    q: queue.Queue = queue.Queue(maxsize=1024)
    with _lock:
        _subs.setdefault(job_id, []).append(q)
    return q


def unsubscribe(job_id: str, q: queue.Queue) -> None:
    with _lock:
        lst = _subs.get(job_id)
        if lst and q in lst:
            lst.remove(q)
            if not lst:
                _subs.pop(job_id, None)


def publish(job_id: str, event: dict[str, Any]) -> None:
    """Fan an event out to every subscriber of ``job_id`` (no-op if none)."""
    with _lock:
        subs = list(_subs.get(job_id, []))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            # Drop events rather than block the render worker if a slow client
            # falls behind; the SSE endpoint keeps the latest coarse status via
            # get_render_status anyway.
            pass


def progress_event(
    job_id: str,
    *,
    phase: str,
    status: Optional[str] = None,
    percent: Optional[float] = None,
    message: Optional[str] = None,
    stage: Optional[str] = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a standardized progression event dict."""
    ev: dict[str, Any] = {
        "event": "render_progress",
        "render_job_id": job_id,
        "phase": phase,
        "ts": time.time(),
    }
    if status is not None:
        ev["status"] = status
    if percent is not None:
        ev["percent"] = round(percent, 1)
    if message is not None:
        ev["message"] = message
    if stage is not None:
        ev["stage"] = stage
    ev.update(extra)
    return ev
