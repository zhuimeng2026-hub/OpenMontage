import json

import pytest

import lib.workbuddy_session as sessions


def _state_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)


def _image(tmp_path, name="one.jpg"):
    path = tmp_path / "projects" / "demo" / "assets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-image")
    return path


def test_find_session_by_job_id_locates_without_scan(monkeypatch, tmp_path):
    """begin_render must register the job in the index so lookups are O(1)."""
    _state_env(monkeypatch, tmp_path)
    asset = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    sessions.register_image("sess1", "demo", asset)
    _, state = sessions.begin_render("sess1")
    job_id = state["render_job_id"]

    # Index file exists and points at the right session.
    index = json.loads(sessions._index_path().read_text(encoding="utf-8"))
    assert index[job_id] == sessions.session_hash("sess1")

    found = sessions.find_session_by_job_id(job_id)
    assert found is not None
    assert found["render_job_id"] == job_id
    assert found["batch_id"] == state["batch_id"]

    # Unknown job id
    assert sessions.find_session_by_job_id("does-not-exist") is None
    assert sessions.find_session_by_job_id("") is None


def test_begin_render_overwrites_stale_index_entry(monkeypatch, tmp_path):
    """A retried batch must drop the previous job_id from the index, not leak it."""
    _state_env(monkeypatch, tmp_path)
    asset = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    sessions.register_image("retry", "demo", asset)
    _, first = sessions.begin_render("retry")
    old_job = first["render_job_id"]
    # Simulate a finished (failed) previous run so begin_render is allowed again.
    sessions.update("retry", status="failed", failure_stage="render", error="boom")
    _, second = sessions.begin_render("retry")
    new_job = second["render_job_id"]

    index = json.loads(sessions._index_path().read_text(encoding="utf-8"))
    assert index[new_job] == sessions.session_hash("retry")
    assert old_job not in index
    assert sessions.find_session_by_job_id(old_job) is None


def test_orphan_recovery_marks_in_flight_as_failed(monkeypatch, tmp_path):
    """rendering/queued sessions become failed/orphaned with a non-empty error."""
    _state_env(monkeypatch, tmp_path)
    sessions.STATE_DIR.mkdir(parents=True, exist_ok=True)
    digest = sessions.session_hash("orphan-session")
    rendering = {
        "project_id": "demo", "batch_id": "batch-1", "status": "rendering",
        "assets": [], "render_job_id": "job-rendering", "video_path": None,
        "share_url": None, "failure_stage": None, "error": None,
    }
    queued = {
        "project_id": "demo", "batch_id": "batch-2", "status": "queued",
        "assets": [], "render_job_id": "job-queued", "video_path": None,
        "share_url": None, "failure_stage": None, "error": None,
    }
    published = {
        "project_id": "demo", "batch_id": "batch-3", "status": "published",
        "assets": [], "render_job_id": "job-published", "video_path": "/x.mp4",
        "share_url": "https://share.example/x", "failure_stage": None, "error": None,
    }
    (sessions.STATE_DIR / f"{digest}.json").write_text(json.dumps(rendering), encoding="utf-8")
    (sessions.STATE_DIR / f"{sessions.session_hash('q')}.json").write_text(json.dumps(queued), encoding="utf-8")
    (sessions.STATE_DIR / f"{sessions.session_hash('p')}.json").write_text(json.dumps(published), encoding="utf-8")

    stats = sessions.recover_orphans_and_rebuild_index()
    assert stats["orphaned"] == 2

    reopened = json.loads((sessions.STATE_DIR / f"{digest}.json").read_text(encoding="utf-8"))
    assert reopened["status"] == "failed"
    assert reopened["failure_stage"] == "orphaned"
    assert reopened["error"] and "restart" in reopened["error"]

    q = json.loads((sessions.STATE_DIR / f"{sessions.session_hash('q')}.json").read_text(encoding="utf-8"))
    assert q["status"] == "failed"
    assert q["failure_stage"] == "orphaned"

    # A published (terminal) session must NOT be touched by recovery.
    p = json.loads((sessions.STATE_DIR / f"{sessions.session_hash('p')}.json").read_text(encoding="utf-8"))
    assert p["status"] == "published"
    assert p["failure_stage"] is None


def test_index_rebuild_after_corruption(monkeypatch, tmp_path):
    """Deleting/corrupting the index is repaired by recovery; lookups still work."""
    _state_env(monkeypatch, tmp_path)
    asset = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    sessions.register_image("rebuild", "demo", asset)
    _, state = sessions.begin_render("rebuild")
    job_id = state["render_job_id"]

    # Destroy the index file entirely.
    sessions._index_path().unlink(missing_ok=True)
    assert not sessions._index_path().exists()
    assert sessions.find_session_by_job_id(job_id) is None

    # Recovery rebuilds it from disk.
    stats = sessions.recover_orphans_and_rebuild_index()
    assert stats["indexed"] >= 1
    assert sessions._index_path().exists()

    found = sessions.find_session_by_job_id(job_id)
    assert found is not None
    assert found["render_job_id"] == job_id


def test_index_rebuild_heals_corrupted_index_file(monkeypatch, tmp_path):
    """A corrupted (non-JSON) index file is overwritten, not crashed on."""
    _state_env(monkeypatch, tmp_path)
    sessions.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sessions._index_path().write_text("{not valid json", encoding="utf-8")

    asset = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    sessions.register_image("heal", "demo", asset)
    _, state = sessions.begin_render("heal")
    job_id = state["render_job_id"]

    # _read_index tolerates corruption, so the lookup works even before recovery.
    assert sessions.find_session_by_job_id(job_id) is not None
