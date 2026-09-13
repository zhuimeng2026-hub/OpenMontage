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
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def _atomic_replace(tmp: Path, path: Path) -> None:
    """os.replace with retry for Windows "Access Denied" (WinError 5)."""
    delay = 0.02
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(delay)
            delay *= 1.7

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "projects" / ".mcp_sessions"
_JOBS_FILENAME = ".render_jobs.json"

_jobs_lock = threading.Lock()


@dataclass(eq=False)
class RenderTicket:
    """One waiting render owned by a stable BFF user/session identity."""

    job_id: str
    owner_id: str


class FairRenderGate:
    """Process-wide fair gate for expensive Remotion processes.

    Jobs remain FIFO within one owner. Across owners, the next free slot is
    handed out round-robin, while ``max_per_owner`` prevents one user from
    occupying every render process. The MCP job record remains the durable
    source of truth; this object only coordinates live worker threads.
    """

    def __init__(self, capacity: int, max_per_owner: int = 1) -> None:
        if capacity < 1:
            raise ValueError("render capacity must be at least 1")
        if max_per_owner < 1:
            raise ValueError("per-owner render capacity must be at least 1")
        self.capacity = capacity
        self.max_per_owner = min(max_per_owner, capacity)
        self._condition = threading.Condition()
        self._waiting: dict[str, deque[RenderTicket]] = {}
        self._owners: deque[str] = deque()
        self._tickets: dict[str, RenderTicket] = {}
        self._active_total = 0
        self._active_by_owner: dict[str, int] = {}

    def enter(self, job_id: str, owner_id: str) -> tuple[RenderTicket, int, int]:
        """Enqueue a unique job and return its ticket, position and depth."""
        owner_id = owner_id or "anonymous"
        with self._condition:
            if job_id in self._tickets:
                raise ValueError(f"render job is already queued: {job_id}")
            ticket = RenderTicket(job_id=job_id, owner_id=owner_id)
            queue = self._waiting.get(owner_id)
            if queue is None:
                queue = deque()
                self._waiting[owner_id] = queue
                self._owners.append(owner_id)
            queue.append(ticket)
            self._tickets[job_id] = ticket
            position = self._position_locked(job_id)
            depth = len(self._tickets)
            self._condition.notify_all()
            return ticket, position or depth, depth

    def acquire(self, ticket: RenderTicket) -> None:
        """Block until ``ticket`` is selected by the per-owner round robin."""
        with self._condition:
            while True:
                if self._tickets.get(ticket.job_id) is not ticket:
                    raise RuntimeError(f"render ticket is no longer queued: {ticket.job_id}")
                owner = self._next_eligible_owner_locked()
                queue = self._waiting.get(ticket.owner_id)
                if (
                    self._active_total < self.capacity
                    and owner == ticket.owner_id
                    and queue
                    and queue[0] is ticket
                ):
                    queue.popleft()
                    del self._tickets[ticket.job_id]
                    self._active_total += 1
                    self._active_by_owner[ticket.owner_id] = self._active_by_owner.get(ticket.owner_id, 0) + 1
                    self._rotate_owner_locked(ticket.owner_id, bool(queue))
                    self._condition.notify_all()
                    return
                self._condition.wait()

    def release(self, owner_id: str) -> None:
        """Release exactly one active slot owned by ``owner_id``."""
        with self._condition:
            active = self._active_by_owner.get(owner_id, 0)
            if active < 1 or self._active_total < 1:
                raise RuntimeError(f"unbalanced render slot release for owner {owner_id!r}")
            self._active_total -= 1
            if active == 1:
                del self._active_by_owner[owner_id]
            else:
                self._active_by_owner[owner_id] = active - 1
            self._condition.notify_all()

    def position(self, job_id: str) -> Optional[int]:
        with self._condition:
            return self._position_locked(job_id)

    def depth(self) -> int:
        with self._condition:
            return len(self._tickets)

    def active(self) -> int:
        with self._condition:
            return self._active_total

    def active_for(self, owner_id: str) -> int:
        with self._condition:
            return self._active_by_owner.get(owner_id, 0)

    def _next_eligible_owner_locked(self) -> Optional[str]:
        if self._active_total >= self.capacity:
            return None
        for owner_id in self._owners:
            if self._waiting.get(owner_id) and self._active_by_owner.get(owner_id, 0) < self.max_per_owner:
                return owner_id
        return None

    def _rotate_owner_locked(self, owner_id: str, still_waiting: bool) -> None:
        try:
            self._owners.remove(owner_id)
        except ValueError:
            pass
        if still_waiting:
            self._owners.append(owner_id)
        else:
            self._waiting.pop(owner_id, None)

    def _position_locked(self, job_id: str) -> Optional[int]:
        """Return a round-robin display position without mutating live state."""
        queues = {owner: deque(items) for owner, items in self._waiting.items() if items}
        owners = deque(owner for owner in self._owners if queues.get(owner))
        position = 0
        while owners:
            owner = owners.popleft()
            queue = queues[owner]
            candidate = queue.popleft()
            position += 1
            if candidate.job_id == job_id:
                return position
            if queue:
                owners.append(owner)
        return None


_fair_gate: Optional[FairRenderGate] = None
_fair_gate_guard = threading.Lock()


def get_fair_render_gate(capacity: int, max_per_owner: int = 1) -> FairRenderGate:
    """Return the lazily-created process gate and reject config drift."""
    global _fair_gate
    with _fair_gate_guard:
        if _fair_gate is None:
            _fair_gate = FairRenderGate(capacity, max_per_owner)
        elif (_fair_gate.capacity, _fair_gate.max_per_owner) != (capacity, min(max_per_owner, capacity)):
            raise RuntimeError("Remotion render limits changed after the process gate was initialized; restart the MCP service")
        return _fair_gate


def fair_render_queue_snapshot(job_id: str) -> tuple[Optional[int], int]:
    """Return a live fair-queue position/depth without creating the gate."""
    with _fair_gate_guard:
        gate = _fair_gate
    if gate is None:
        return None, 0
    return gate.position(job_id), gate.depth()


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
            _atomic_replace(tmp, path)
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
                _atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def all_job_records() -> dict[str, dict[str, Any]]:
    with _jobs_lock:
        return _load_records_nolock()
