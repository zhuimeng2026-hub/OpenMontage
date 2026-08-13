"""Tests for the process-wide render queue (waiting-set tracking + persistence)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

# Ensure repo root on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import render_queue


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Point the module's STATE_DIR at a temp dir so record I/O is isolated."""
    monkeypatch.setattr(render_queue, "STATE_DIR", tmp_path)
    return tmp_path


def test_render_queue_fifo_position_depth():
    q = render_queue.RenderQueue()
    assert q.depth() == 0
    p1, d1 = q.enter("a")
    assert (p1, d1) == (1, 1)
    p2, d2 = q.enter("b")
    assert (p2, d2) == (2, 2)
    # Late joiner queues behind.
    p3, d3 = q.enter("c")
    assert (p3, d3) == (3, 3)
    # Leaving the head shifts everyone up.
    q.leave("a")
    assert q.position("b") == 1
    assert q.position("c") == 2
    assert q.depth() == 2
    # Re-entering a known id is idempotent (no duplicate).
    pb, db = q.enter("b")
    assert (pb, db) == (1, 2)
    q.leave("b")
    q.leave("c")
    assert q.depth() == 0
    assert q.position("b") is None


def test_render_queue_threaded_concurrent_enter(tmp_state):
    """Concurrent enters must never lose or duplicate a waiting job."""
    q = render_queue.RenderQueue()
    ids = [f"job-{i}" for i in range(50)]

    def worker(jid):
        q.enter(jid)

    threads = [threading.Thread(target=worker, args=(jid,)) for jid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert q.depth() == 50
    snap = q.snapshot()
    assert sorted(snap) == sorted(ids)
    assert len(snap) == len(set(snap))  # no duplicates


def test_job_record_save_load_delete_roundtrip(tmp_state):
    rec = {
        "sid": "abc", "job_id": "j1", "safe_assets": [{"id": "x", "path": "/p"}],
        "edit_decisions": {"cuts": [{"id": "c1"}]}, "profile": "tiktok",
        "output": "/out.mp4", "title": None, "asset_count": 1,
    }
    render_queue.save_job_record("j1", rec)
    assert render_queue.load_job_record("j1") == rec
    # Second save overwrites, doesn't duplicate.
    render_queue.save_job_record("j2", {"job_id": "j2"})
    assert render_queue.load_job_record("j1") is not None
    assert render_queue.load_job_record("j2") is not None
    render_queue.delete_job_record("j1")
    assert render_queue.load_job_record("j1") is None
    assert render_queue.load_job_record("j2") is not None
    render_queue.delete_job_record("j2")
    assert render_queue.all_job_records() == {}


def test_job_record_atomic_write_survives_corrupt_file(tmp_state):
    # A pre-existing corrupt records file must be treated as empty, not crash.
    (tmp_state / render_queue._JOBS_FILENAME).write_text("{not json", encoding="utf-8")
    assert render_queue.all_job_records() == {}
    render_queue.save_job_record("jx", {"job_id": "jx", "k": 1})
    assert render_queue.load_job_record("jx") == {"job_id": "jx", "k": 1}
