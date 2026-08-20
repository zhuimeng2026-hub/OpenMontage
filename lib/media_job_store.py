"""Durable state for non-photo media workflows exposed through MCP."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "projects" / ".media_jobs.json"
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, dict[str, Any]]:
    if not STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(records: dict[str, dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_name(f".{STORE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STORE_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def create_job(
    *, session_hash: str, project_id: str, job_type: str,
    title: str | None = None, metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not session_hash:
        raise ValueError("session_hash is required")
    if not project_id:
        raise ValueError("project_id is required")
    job_id = uuid.uuid4().hex
    stamp = _now()
    record = {
        "job_id": job_id, "render_job_id": job_id,
        "session_hash": session_hash, "project_id": project_id,
        "job_type": job_type, "title": title or "",
        "status": "queued", "current_stage": "queued", "progress": 0,
        "executor": "openmontage", "executor_job_id": None,
        "executor_worker_id": None, "result_url": None, "video_path": None,
        "error_code": None, "error_message": None,
        "metadata": metadata or {}, "created_at": stamp, "updated_at": stamp,
    }
    with _lock:
        records = _read()
        records[job_id] = record
        _write(records)
    return dict(record)


def get_job(job_id: str, *, session_hash: str | None = None) -> dict[str, Any] | None:
    with _lock:
        record = _read().get(job_id)
    if not record or (session_hash and record.get("session_hash") != session_hash):
        return None
    return dict(record)


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    with _lock:
        records = _read()
        if job_id not in records:
            raise KeyError(f"unknown media job: {job_id}")
        record = records[job_id]
        record.update(changes)
        record["updated_at"] = _now()
        records[job_id] = record
        _write(records)
        return dict(record)


def list_jobs(session_hash: str, *, limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        records = [dict(v) for v in _read().values() if v.get("session_hash") == session_hash]
    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return records[: max(1, min(int(limit), 500))]


def recover_incomplete_jobs() -> int:
    """Mark jobs whose daemon workers disappeared during restart as failed."""
    terminal = {"published", "failed"}
    recovered = 0
    with _lock:
        records = _read()
        for record in records.values():
            if record.get("status") not in terminal:
                previous = record.get("current_stage") or record.get("status") or "queued"
                record.update({
                    "status": "failed",
                    "current_stage": "orphaned",
                    "error_code": "orphaned",
                    "error_message": f"server restarted while job was in stage {previous}; retry the task",
                    "updated_at": _now(),
                })
                recovered += 1
        if recovered:
            _write(records)
    return recovered
