"""Thread-safe in-memory job store + Job dataclass.

Shared between Feature A (SSE progress) and Feature B (this queue).
Feature A reads via get_store().get(job_id); Feature B writes via update().
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class Job:
    id: str
    project_id: str
    status: str = "queued"  # queued | rendering | completed | failed
    percent: Optional[float] = None
    phase: Optional[str] = None
    message: Optional[str] = None
    staging_id: Optional[str] = None
    output_path: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, project_id: str, job_id: Optional[str] = None) -> Job:
        import uuid
        job = Job(id=job_id or uuid.uuid4().hex, project_id=project_id)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for k, v in fields.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            job.updated_at = time.time()
            return job

    def list_for_project(self, project_id: str, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.project_id == project_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]


_store: JobStore | None = None
_store_lock = threading.Lock()


def get_store() -> JobStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = JobStore()
        return _store