"""Session-scoped photo batches for the WorkBuddy MCP workflow.

The MCP session id is only used as an opaque correlation key.  Raw ids never
reach disk or logs; state writes use replace-on-same-filesystem semantics.
"""
from __future__ import annotations

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


def _state_path(digest: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{digest}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(digest: str) -> dict[str, Any] | None:
    path = _state_path(digest)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def locked(session_id: str | None) -> Iterator[tuple[str, dict[str, Any] | None]]:
    digest = require_session(session_id)
    lock = _lock_for(digest)
    with lock:
        yield digest, _read(digest)


def register_image(session_id: str | None, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Add one completed image, deduplicating by sha/path within the open batch."""
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest)
        if state and state.get("status") == "rendering":
            raise ValueError("MCP session batch is currently rendering; upload after it completes")
        if not state or state.get("status") == "published":
            now = _now()
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
            }
        elif state.get("project_id") != project_id:
            raise ValueError("MCP session is already collecting assets for another project")

        digest_value = asset.get("sha256")
        path_value = asset.get("path")
        if not any(
            (digest_value and item.get("sha256") == digest_value)
            or (path_value and item.get("path") == path_value)
            for item in state["assets"]
        ):
            state["assets"].append(asset)
        _write(_state_path(digest), state)
        return state


def begin_render(session_id: str | None, project_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """Atomically claim the current batch for one render job."""
    digest = require_session(session_id)
    with _lock_for(digest):
        state = _read(digest)
        if not state:
            raise ValueError("No uploaded image batch found for this MCP session")
        if state.get("status") == "rendering":
            raise ValueError("A video is already being generated for this MCP session")
        if state.get("status") == "published":
            raise ValueError("The current image batch is already published; upload a new image first")
        if project_id and state.get("project_id") != project_id:
            raise ValueError("project_id does not match the current MCP session batch")
        if not state.get("assets"):
            raise ValueError("No completed images found in the current MCP session batch")
        state["status"] = "rendering"
        state["render_job_id"] = uuid.uuid4().hex
        _write(_state_path(digest), state)
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


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return state safe for responses/logging; asset paths are still local paths."""
    return state
