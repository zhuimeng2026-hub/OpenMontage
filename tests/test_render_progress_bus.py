"""Tests for the live render-progress pub/sub bus."""

import time

from lib.render_progress import (
    progress_event,
    publish,
    subscribe,
    unsubscribe,
)


def test_publish_reaches_subscriber():
    q = subscribe("job-x")
    try:
        publish("job-x", progress_event("job-x", phase="render", percent=42.0))
        ev = q.get(timeout=1.0)
        assert ev["render_job_id"] == "job-x"
        assert ev["phase"] == "render"
        assert ev["percent"] == 42.0
        assert ev["event"] == "render_progress"
        assert "ts" in ev
    finally:
        unsubscribe("job-x", q)


def test_publish_to_unsubscribed_job_is_noop():
    # No subscriber registered → must not raise.
    publish("ghost", progress_event("ghost", phase="render"))


def test_unsubscribe_cleans_up_when_last():
    q = subscribe("job-y")
    unsubscribe("job-y", q)
    # Re-subscribing for the same id should get a fresh empty queue (no leak).
    q2 = subscribe("job-y")
    assert q2.empty()
    unsubscribe("job-y", q2)


def test_subscriber_lists_are_per_job_isolated():
    qa = subscribe("a")
    qb = subscribe("b")
    try:
        publish("a", progress_event("a", phase="render", status="rendering"))
        # 'b' queue must not receive 'a' events
        assert qb.empty()
        ev = qa.get(timeout=1.0)
        assert ev["render_job_id"] == "a"
    finally:
        unsubscribe("a", qa)
        unsubscribe("b", qb)


def test_progress_event_defaults_and_overrides():
    ev = progress_event("j", phase="share", status="published", share_url="https://x")
    assert ev["phase"] == "share"
    assert ev["status"] == "published"
    assert ev["share_url"] == "https://x"
    assert "percent" not in ev  # only included when provided
