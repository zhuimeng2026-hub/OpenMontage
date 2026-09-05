"""Session-scoped photo batches for the WorkBuddy MCP workflow.

The MCP session id is only used as an opaque correlation key.  Raw ids never
reach disk or logs; state writes use replace-on-same-filesystem semantics.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX-only advisory lock; missing on Windows so the import still works.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "projects" / ".mcp_sessions"
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def session_hash(session_id: str | None) -> str | None:
    if not session_id or not str(session_id).strip():
        return None
    return hashlib.sha256(str(session_id).strip().encode("utf-8")).hexdigest()[:16]


def require_session(session_id: str | None) -> str:
    digest = session_hash(session_id)
    if not digest:
        raise ValueError("Streamable HTTP Mcp-Session-Id is required for this workflow")
    return digest


def _lock_for(digest: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(digest, threading.RLock())


# Cross-process file lock directory. ``flock`` (POSIX advisory lock) is the
# real safety net for multi-worker / multi-container deployments: the
# in-process ``_locks`` RLock above only protects within a single Python
# interpreter. Two concurrent MCP worker processes hitting the same session
# would otherwise race on _read / _write / register_image, and the chunked
# upload dedup path in ``asset_upload_chunk.py`` can also delete files based
# on stale canonical metadata.
_LOCK_DIR = STATE_DIR / ".locks"  # compatibility alias; use _lock_dir() below


def _lock_dir() -> Path:
    """Return the lock directory for the current (possibly patched) state root."""
    return Path(STATE_DIR) / ".locks"


@contextmanager
def _flock_for(digest: str) -> Iterator[None]:
    """Acquire a POSIX advisory lock scoped to one session digest.

    Falls back to a no-op on platforms without fcntl.flock (Windows) so the
    import doesn't break dev workflows; the in-process RLock still applies.
    """
    lock_dir = _lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{digest}.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # Best-effort: single-process deployments still get in-process
                # safety from ``_lock_for``. Logged at debug level only.
                pass
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _state_path(digest: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{digest}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_replace(tmp: Path, target: Path) -> None:
    """Replace a state file, tolerating short Windows scanner/reader locks.

    On Windows the destination can be transiently locked by antivirus or a
    concurrent reader, making os.replace fail with PermissionError. A short
    back-off retry is the standard remedy; we never mask a genuinely broken
    filesystem because the final attempt re-raises the real OSError. The
    back-off starts at 20 ms and grows linearly (≈ up to ~180 ms total) which
    is short enough not to keep the caller waiting under normal contention
    but long enough to ride out an AV scan window.
    """
    for attempt in range(10):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


def _write(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(digest: str) -> dict[str, Any] | None:
    path = _state_path(digest)
    if not path.exists():
        return None
    # Windows may briefly deny a reader while another thread completes the
    # atomic replace. Retry the tiny hand-off window instead of surfacing a
    # transient PermissionError to status polling.
    for attempt in range(4):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.01)
    return None


# ---------------------------------------------------------------------------
# Job-id → session index
# ---------------------------------------------------------------------------
# Maps ``render_job_id -> digest`` so get_render_status can locate a session in
# O(1) instead of scanning every session file on disk. The index is a single
# JSON file written atomically (tmp + os.replace) under STATE_DIR. A module-level
# lock serialises index mutations because the index is shared across all
# sessions, unlike the per-digest file locks.
_INDEX_FILENAME = ".job_index.json"
_index_lock = threading.Lock()


def _index_path() -> Path:
    return STATE_DIR / _INDEX_FILENAME


def _read_index() -> dict[str, str]:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupted index is treated as empty; recovery rebuilds it from disk.
        return {}


def _write_index(index: dict[str, str]) -> None:
    path = _index_path()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _index_upsert(job_id: str, digest: str, old_job_id: str | None = None) -> None:
    """Map ``job_id -> digest`` in the index (removing a superseded id).

    ``old_job_id`` is the previous render_job_id for the same session, deleted
    so a retried batch does not leave a stale pointer to an overwritten job.
    """
    if not job_id:
        return
    with _index_lock:
        index = _read_index()
        if old_job_id and index.get(old_job_id) == digest:
            del index[old_job_id]
        index[job_id] = digest
        _write_index(index)


def find_session_by_job_id(job_id: str) -> dict[str, Any] | None:
    """Locate a session's state dict directly via the job→session index.

    Returns the latest state dict on disk, or None when ``job_id`` is unknown.
    O(1) index lookup — no full-directory scan.
    """
    if not job_id:
        return None
    with _index_lock:
        digest = _read_index().get(job_id)
    if not digest:
        return None
    return _read(digest)


def update_session_by_job_id(job_id: str, **changes: Any) -> dict[str, Any] | None:
    """Atomically update a persisted session resolved by its render job id.

    Publish retry callers only have the opaque job id, not the raw MCP session
    id.  Resolve it through the durable index and reuse the per-session lock
    and atomic writer used by the normal session APIs.
    """
    if not job_id:
        return None
    with _index_lock:
        digest = _read_index().get(job_id)
    if not digest:
        return None
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            return None
        state.update(changes)
        _write(_state_path(digest), state)
        return state


# Statuses that mean a render was in-flight when the process died.
_ORPHAN_STATUSES = frozenset({"rendering", "queued", "rendered", "uploading", "sharing"})

# render_phase value meaning "claimed a slot but still waiting for a Remotion
# render slot (blocked on the semaphore)". Such jobs have NOT started
# ``remotion render`` yet, so they are safe to re-enqueue after a restart
# instead of being marked failed.
_REQUEUE_PHASE = "queued_for_slot"

# The durable job-record file written by lib.render_queue; recovery must skip
# it when scanning STATE_DIR so it is neither treated as a session nor indexed.
_JOBS_FILENAME = ".render_jobs.json"


def recover_orphans_and_rebuild_index() -> dict[str, int]:
    """Startup maintenance: re-enqueue waiting jobs, fail active ones, rebuild index.

    A session left in an active render/publish status was interrupted by a server
    crash/restart — the daemon thread that would finish it is gone. Two cases:

    * ``render_phase == 'queued_for_slot'`` — the job was *waiting for a render
      slot*, not actually rendering. Its durable job record (lib.render_queue)
      is still on disk, so it is re-enqueued: the caller (mcp_server startup)
      re-dispatches it. It is NOT marked failed.
    * otherwise (actively rendering, or no phase) — marked ``status='failed'``
      with ``failure_stage='orphaned'``. Renders are NOT auto-restarted because
      that would double-render and waste resources.

    The job→session index is rebuilt from disk so it reflects reality (and
    self-heals a corrupted/missing index).
    """
    orphaned = 0
    requeued: list[str] = []
    index: dict[str, str] = {}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    index_path = _index_path()
    for path in STATE_DIR.glob("*.json"):
        if path == index_path or path.name == _JOBS_FILENAME:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        digest = path.stem
        if data.get("status") in _ORPHAN_STATUSES:
            if data.get("render_phase") == _REQUEUE_PHASE:
                # Waiting for a slot, not rendering: keep for re-dispatch.
                requeued.append(data.get("render_job_id"))
            else:
                data["status"] = "failed"
                data["failure_stage"] = "orphaned"
                data["error"] = "interrupted by server restart; please retry the render"
                # Do not leave stale queue metadata on an orphaned job.  A
                # worker that had already acquired a slot (or was in the
                # upload/share phase) can no longer be in the live queue after
                # a process restart, so exposing its old phase/position would
                # make the status endpoint report contradictory state.
                data["render_phase"] = None
                data["queue_position"] = None
                data["queue_depth"] = None
                try:
                    _write(path, data)
                except OSError:
                    pass
                orphaned += 1
        render_job_id = data.get("render_job_id")
        if render_job_id:
            index[render_job_id] = digest
    _write_index(index)
    return {"orphaned": orphaned, "indexed": len(index), "requeued": len(requeued),
            "_requeued_ids": requeued}


@contextmanager
def locked(session_id: str | None) -> Iterator[tuple[str, dict[str, Any] | None]]:
    digest = require_session(session_id)
    lock = _lock_for(digest)
    with lock:
        yield digest, _read(digest)


def get_session_assets(session_id: str | None) -> list[dict[str, Any]]:
    """Return the images already uploaded for an MCP session.

    Used by the frontend to show what is already on the server so the user
    does not re-upload files after a partial upload failure. Returns [] when
    the session has no state yet (nothing uploaded) or the id is missing.
    """
    digest = session_hash(session_id)
    if not digest:
        return []
    state = _read(digest)
    if not state:
        return []
    return list(state.get("assets", []))


def _register_asset(session_id: str | None, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Register one completed asset in the session's current asset batch.

    This is deliberately shared by the legacy image-batch API and the generic
    asset API.  Keeping the state shape and transition rules in one place is
    important: existing image uploads continue to participate in the same
    Remotion batch while video/audio uploads become discoverable by asset id.

    Holds BOTH an in-process RLock (cheap, nested-safe) AND a POSIX advisory
    flock on ``.locks/<digest>.lock`` so that two MCP worker processes (or
    multiple BFF instances) cannot race on the same session file. The flock
    is the real cross-process safety net; the RLock prevents recursive
    deadlock within a single worker.
    """
    digest = require_session(session_id)
    with _lock_for(digest), _flock_for(digest):
        state = _read(digest)
        if state and state.get("status") == "rendering":
            raise ValueError("MCP session batch is currently rendering; upload after it completes")
        if not state or state.get("status") == "published" or (
            state.get("status") == "idle" and not state.get("assets")
        ):
            now = _now()
            media_assets = (state or {}).get("media_assets", {})
            state = {
                "project_id": project_id,
                "batch_id": uuid.uuid4().hex,
                "status": "collecting_assets",
                "assets": [],
                "created_at": now,
                "updated_at": now,
                "render_job_id": None,
                "video_path": None,
                "share_url": None,
                "media_assets": media_assets,
            }
        elif state.get("project_id") != project_id:
            raise ValueError("MCP session is already collecting assets for another project")

        digest_value = asset.get("sha256")
        # Prefer the OS-portable relative_path; fall back to path for legacy
        # session state that predates the relative_path field.
        rel_value = asset.get("relative_path") or asset.get("path")
        if not any(
            (digest_value and item.get("sha256") == digest_value)
            or (rel_value and (item.get("relative_path") or item.get("path")) == rel_value)
            for item in state["assets"]
        ):
            state["assets"].append(asset)
        _write(_state_path(digest), state)
        return state


def register_asset(session_id: str | None, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Register media without coupling it to the legacy single photo batch.

    Images retain the original batch semantics. Video/audio assets live in a
    per-project map so one long-lived MCP session can submit multiple media
    jobs without tripping the photo batch's project/status state machine.
    """
    if asset.get("type") == "image":
        return _register_asset(session_id, project_id, asset)
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest) or {"status": "idle", "assets": [], "created_at": _now()}
        media_assets = state.setdefault("media_assets", {})
        project_assets = media_assets.setdefault(project_id, [])
        digest_value = asset.get("sha256")
        path_value = asset.get("path")
        if not any(
            (digest_value and item.get("sha256") == digest_value)
            or (path_value and item.get("path") == path_value)
            for item in project_assets
        ):
            project_assets.append(asset)
        _write(_state_path(digest), state)
        return state


def register_image(session_id: str | None, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Add one completed image, preserving the legacy photo-batch behavior."""
    return _register_asset(session_id, project_id, asset)


def replace_asset_by_sha(
    session_id: str | None, project_id: str, asset: dict[str, Any]
) -> dict[str, Any] | None:
    """Replace an existing asset entry whose sha256 matches ``asset.sha256``.

    Used by the chunked-upload dedup path when the canonical file referenced
    by an existing entry is missing on disk (cleanup job / RepoRoot mismatch
    / earlier race): the new upload must promote itself into the same slot
    instead of silently deleting its own bytes. ``register_image`` cannot be
    used for this because its sha-dedup rule refuses to append a duplicate.

    Returns the rewritten state on success, or None when no matching sha
    was found (caller can fall through to register_image for a clean
    append). Cross-process safe: same flock + RLock as register_image.
    """
    digest = require_session(session_id)
    target_sha = asset.get("sha256")
    if not target_sha:
        return None
    with _lock_for(digest), _flock_for(digest):
        state = _read(digest)
        if not state:
            return None
        if state.get("project_id") != project_id:
            raise ValueError("MCP session is already collecting assets for another project")
        assets_list = state.get("assets")
        if not isinstance(assets_list, list):
            return None
        for i, item in enumerate(assets_list):
            if isinstance(item, dict) and item.get("sha256") == target_sha:
                assets_list[i] = asset
                _write(_state_path(digest), state)
                return state
        return None


def find_asset(
    session_id: str | None,
    asset_id: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """Find one registered asset by id within a session.

    ``project_id`` is an optional scope check.  A missing session, unknown id,
    or project mismatch returns ``None`` so callers can safely treat this as a
    lookup rather than having to catch state errors.
    """
    digest = require_session(session_id)
    if not isinstance(asset_id, str) or not asset_id:
        return None
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            return None
        candidates: list[dict[str, Any]] = []
        if project_id is None or state.get("project_id") == project_id:
            candidates.extend(state.get("assets", []))
        media_assets = state.get("media_assets", {})
        if project_id is None:
            for items in media_assets.values():
                candidates.extend(items)
        else:
            candidates.extend(media_assets.get(project_id, []))
        for asset in candidates:
            if asset.get("id") == asset_id or asset.get("asset_id") == asset_id:
                return asset
    return None


def list_assets(
    session_id: str | None,
    project_id: str | None = None,
    asset_type: str | None = None,
) -> list[dict[str, Any]]:
    """List registered assets, optionally scoped by project and media type."""
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            return []
        assets: list[dict[str, Any]] = []
        if project_id is None or state.get("project_id") == project_id:
            assets.extend(state.get("assets", []))
        media_assets = state.get("media_assets", {})
        if project_id is None:
            for items in media_assets.values():
                assets.extend(items)
        else:
            assets.extend(media_assets.get(project_id, []))
        if asset_type is None:
            return list(assets)
        return [asset for asset in assets if asset.get("type") == asset_type]


def begin_render(
    session_id: str | None,
    project_id: str | None = None,
    *,
    allow_continue: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Atomically claim the current batch for one render job.

    ``allow_continue=True`` relaxes the ``status=="published"`` rejection so
    multi-chunk pagination (see ``create_remotion_video_share``'s
    ``assets_offset`` / ``assets_limit``) can start a fresh render after the
    previous chunk already published. The ``status=="rendering"`` guard is
    *not* relaxed — concurrent chunks on the same session are still refused.
    """
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            raise ValueError("No uploaded image batch found for this MCP session")
        if state.get("status") == "rendering":
            raise ValueError("A video is already being generated for this MCP session")
        if state.get("status") == "published" and not allow_continue:
            raise ValueError("The current image batch is already published; upload a new image first")
        if project_id and state.get("project_id") != project_id:
            raise ValueError("project_id does not match the current MCP session batch")
        if not state.get("assets"):
            raise ValueError("No completed images found in the current MCP session batch")
        old_job_id = state.get("render_job_id")
        state["status"] = "rendering"
        state["render_job_id"] = uuid.uuid4().hex
        # Clear any failure markers left by a previous run of this batch so a
        # retried job reports a clean state to get_render_status.
        state["failure_stage"] = None
        state["error"] = None
        _write(_state_path(digest), state)
        # Keep the job→session index in sync so get_render_status stays O(1).
        _index_upsert(state["render_job_id"], digest, old_job_id=old_job_id)
        return digest, state


def update(session_id: str | None, **changes: Any) -> dict[str, Any]:
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            raise ValueError("MCP session batch not found")
        state.update(changes)
        _write(_state_path(digest), state)
        return state


def fail_job_by_id(job_id: str, *, stage: str = "orphaned", error: str) -> bool:
    """Mark the session owning ``job_id`` as failed, resolved via the index.

    Used by startup re-dispatch when a requeued job's durable record is missing
    (cannot be re-run), so the stuck session does not sit in ``queued_for_slot``
    forever. Returns True if a session was updated.
    """
    if not job_id:
        return False
    with _index_lock:
        digest = _read_index().get(job_id)
    if not digest:
        return False
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            return False
        state["status"] = "failed"
        state["failure_stage"] = stage
        state["error"] = error
        state["render_phase"] = None
        _write(_state_path(digest), state)
    return True


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return state safe for responses/logging; asset paths are still local paths."""
    return state
