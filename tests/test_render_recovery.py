"""Tests for restart recovery of waiting vs actively-rendering jobs."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import workbuddy_session as wbs


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(wbs, "STATE_DIR", tmp_path)
    return tmp_path


def _write_session(tmp_state, digest, status, render_phase=None, job_id=None):
    data = {"project_id": "p", "batch_id": "b", "status": status,
            "assets": [], "render_job_id": job_id}
    if render_phase is not None:
        data["render_phase"] = render_phase
    (tmp_state / f"{digest}.json").write_text(json.dumps(data), encoding="utf-8")


def test_recover_requeues_waiting_renders_fails_active(tmp_state):
    # A job merely waiting for a slot at shutdown -> re-enqueue (not failed).
    _write_session(tmp_state, "waitdigest", "rendering",
                   render_phase="queued_for_slot", job_id="wait1")
    # A job actively rendering at shutdown -> mark failed (avoid double-render).
    _write_session(tmp_state, "actdigest", "rendering", job_id="act1")
    # A stale queued (legacy) session without render_phase -> failed (safe).
    _write_session(tmp_state, "legdigest", "queued", job_id="leg1")

    stats = wbs.recover_orphans_and_rebuild_index()

    assert stats["orphaned"] == 2  # act1 + leg1
    assert stats["requeued"] == 1
    assert stats["_requeued_ids"] == ["wait1"]

    # Waiting job left intact for re-dispatch.
    wait = json.loads((tmp_state / "waitdigest.json").read_text())
    assert wait["status"] == "rendering"
    assert wait["render_phase"] == "queued_for_slot"

    # Active job marked failed/orphaned.
    act = json.loads((tmp_state / "actdigest.json").read_text())
    assert act["status"] == "failed"
    assert act["failure_stage"] == "orphaned"

    # Index rebuilt for the surviving job id.
    assert wbs.find_session_by_job_id("wait1")["batch_id"] == "b"


def test_recover_skips_index_and_jobs_files(tmp_state):
    # The durable job-record file must not be treated as a session.
    (tmp_state / wbs._JOBS_FILENAME).write_text(json.dumps({"j": {"k": 1}}), encoding="utf-8")
    (tmp_state / ".job_index.json").write_text(json.dumps({"x": "y"}), encoding="utf-8")
    _write_session(tmp_state, "d1", "rendering", render_phase="queued_for_slot", job_id="j1")

    stats = wbs.recover_orphans_and_rebuild_index()
    assert stats["requeued"] == 1
    # Records file untouched.
    assert json.loads((tmp_state / wbs._JOBS_FILENAME).read_text()) == {"j": {"k": 1}}


def test_fail_job_by_id(tmp_state):
    _write_session(tmp_state, "dd", "rendering", render_phase="queued_for_slot", job_id="jb")
    # Index points job -> digest so fail_job_by_id can resolve it.
    wbs._write_index({"jb": "dd"})
    ok = wbs.fail_job_by_id("jb", stage="orphaned", error="lost record")
    assert ok is True
    data = json.loads((tmp_state / "dd.json").read_text())
    assert data["status"] == "failed"
    assert data["render_phase"] is None
    # Unknown job -> False, no crash.
    assert wbs.fail_job_by_id("nope", error="x") is False
