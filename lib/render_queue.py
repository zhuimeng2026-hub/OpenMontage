"""Process-wide render queue: waiting-set tracking + job-record persistence.

Two concerns, both needed for a multi-user, queue-backed render service:

1. **Position/depth observability (#1)** — a thread-safe FIFO deque of job ids
   currently *waiting for a Remotion render slot*. Clients can thus see
   ``"you are #N of M"`` instead of a bare "queued". The deque is in-memory
   only; it is rebuilt on restart from the durable job records (see below).

2. **Restart recovery (#2)** — persist each dispatched job's kwargs to disk so
   that, after a crash/restart, jobs that were *waiting for a slot* (not yet
   running ``remotion render``) can be re-dispatched instead of being marked
   failed. The actual slot concurrency limit stays in
   ``tools/video/video_compose.py`` (``_get_remotion_render_semaphore``); this
   module never gates concurrency, it only books the waiting set and the
   durable records.

The in-memory deque and the durable records are intentionally independent:
the deque drives live position/depth display, while the records drive
recovery. A crash loses the deque (harmless — process is gone) but keeps the
records, which is what matters.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "projects" / ".mcp_sessions"
_JOBS_FILENAME = ".render_jobs.json"

_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Waiting-set tracking (in-memory, live position/depth)
# ---------------------------------------------------------------------------
class RenderQueue:
    """FIFO waiting set of job ids blocked on the Remotion slot semaphore."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: list[str] = []

    def enter(self, job_id: str) -> tuple[int, int]:
        """Register ``job_id`` as waiting; return ``(1-based position, depth)``."""
        with self._lock:
            if job_id not in self._waiting:
                self._waiting.append(job_id)
            return self._waiting.index(job_id) + 1, len(self._waiting)

    def leave(self, job_id: str) -> None:
        """Remove ``job_id`` from the waiting set (slot acquired or aborted)."""
        with self._lock:
            if job_id in self._waiting:
                self._waiting.remove(job_id)

    def position(self, job_id: str) -> Optional[int]:
        with self._lock:
            if job_id in self._waiting:
                return self._waiting.index(job_id) + 1
            return None

    def depth(self) -> int:
        with self._lock:
            return len(self._waiting)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._waiting)

    def clear(self) -> None:
        with self._lock:
            self._waiting.clear()


_queue: Optional[RenderQueue] = None
_q_guard = threading.Lock()


def get_render_queue() -> RenderQueue:
    global _queue
    with _q_guard:
        if _queue is None:
            _queue = RenderQueue()
        return _queue


# ---------------------------------------------------------------------------
# Job-record persistence (durable, for restart re-dispatch)
# ---------------------------------------------------------------------------
def _jobs_path() -> Path:
    return STATE_DIR / _JOBS_FILENAME


def _load_records_nolock() -> dict[str, dict[str, Any]]:
    path = _jobs_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_job_record(job_id: str, kwargs: dict[str, Any]) -> None:
    """Persist the dispatch kwargs so a *waiting* job can be re-dispatched.

    Called once in ``create_remotion_video_share`` right before the background
    thread is spawned. The record lives until the job reaches a terminal state
    (``delete_job_record``), so a crash while the job is still queued_for_slot
    leaves a recoverable record on disk.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _jobs_path()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _jobs_lock:
            records = _load_records_nolock()
            records[job_id] = kwargs
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(records, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_job_record(job_id: str) -> Optional[dict[str, Any]]:
    with _jobs_lock:
        return _load_records_nolock().get(job_id)


def delete_job_record(job_id: str) -> None:
    """Drop a job record once the job is terminal (success or failure)."""
    path = _jobs_path()
    if not path.exists():
        return
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _jobs_lock:
            records = _load_records_nolock()
            if job_id in records:
                del records[job_id]
                with tmp.open("w", encoding="utf-8") as handle:
                    json.dump(records, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def all_job_records() -> dict[str, dict[str, Any]]:
    with _jobs_lock:
        return _load_records_nolock()
