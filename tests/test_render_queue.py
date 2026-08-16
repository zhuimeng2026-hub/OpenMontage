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


def test_fair_gate_splits_two_slots_between_two_users():
    gate = render_queue.FairRenderGate(capacity=2, max_per_owner=1)
    a1, _, _ = gate.enter("a1", "alice")
    gate.acquire(a1)

    a2, _, _ = gate.enter("a2", "alice")
    b1, _, _ = gate.enter("b1", "bob")
    a2_acquired = threading.Event()
    b1_acquired = threading.Event()

    def wait_for(ticket, acquired):
        gate.acquire(ticket)
        acquired.set()

    ta = threading.Thread(target=wait_for, args=(a2, a2_acquired), daemon=True)
    tb = threading.Thread(target=wait_for, args=(b1, b1_acquired), daemon=True)
    ta.start()
    tb.start()

    assert b1_acquired.wait(1), "the second user should receive the free global slot"
    assert not a2_acquired.is_set(), "one user must not occupy both slots"
    assert gate.active() == 2
    assert gate.active_for("alice") == 1
    assert gate.active_for("bob") == 1

    gate.release("alice")
    assert a2_acquired.wait(1), "the queued Alice job should start after Alice releases"
    gate.release("bob")
    gate.release("alice")
    ta.join(1)
    tb.join(1)


def test_fair_gate_preserves_fifo_within_each_user():
    gate = render_queue.FairRenderGate(capacity=1, max_per_owner=1)
    blocker, _, _ = gate.enter("blocker", "busy")
    gate.acquire(blocker)
    a1, _, _ = gate.enter("a1", "alice")
    a2, _, _ = gate.enter("a2", "alice")
    b1, _, _ = gate.enter("b1", "bob")

    assert gate.position("a1") == 1
    assert gate.position("b1") == 2
    assert gate.position("a2") == 3
    assert gate.depth() == 3

    gate.release("busy")
    gate.acquire(a1)
    gate.release("alice")
    gate.acquire(b1)
    gate.release("bob")
    gate.acquire(a2)
    gate.release("alice")
    assert gate.depth() == 0


def test_fair_gate_rejects_unbalanced_release():
    gate = render_queue.FairRenderGate(capacity=2, max_per_owner=1)
    with pytest.raises(RuntimeError, match="unbalanced"):
        gate.release("alice")


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
