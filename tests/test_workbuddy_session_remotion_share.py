import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from tools.base_tool import ToolResult
import lib.workbuddy_session as sessions
from lib.mcp_session import reset_mcp_session_id, set_mcp_session_id


def _state_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)


def _image(tmp_path, name="one.jpg"):
    path = tmp_path / "projects" / "demo" / "assets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-image")
    return path


def _install_fakes(monkeypatch, tmp_path, *, fail_upload=False, fail_share=False):
    """Monkeypatch the registry so create_remotion_video_share runs end-to-end
    with instant fake tools. Returns the mcp_server module for polling."""
    import mcp_server

    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)

    class FakeCompose:
        def execute(self, inputs):
            assert inputs["operation"] == "render"
            assert inputs["edit_decisions"]["render_runtime"] == "remotion"
            assert inputs["edit_decisions"]["renderer_family"] == "animation-first"
            assert inputs["profile"] == "tiktok"
            assert len(inputs["edit_decisions"]["cuts"]) == 2
            assert inputs["edit_decisions"]["cuts"][0]["in_seconds"] == 0
            assert inputs["edit_decisions"]["cuts"][0]["out_seconds"] == 3
            assert inputs["edit_decisions"]["cuts"][1]["in_seconds"] == 3
            assert inputs["edit_decisions"]["cuts"][1]["out_seconds"] == 6
            Path(inputs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(inputs["output_path"]).write_bytes(b"mp4")
            return ToolResult(True, {"output": inputs["output_path"]})

    class FakeUpload:
        def execute(self, inputs):
            assert inputs["video_path"].endswith(".mp4")
            if fail_upload:
                return ToolResult(False, error="mock upload failure")
            return ToolResult(True, {"file_id": "file-1"})

    class FakeShare:
        def execute(self, inputs):
            # weiyun_share_link.execute accepts a list of file-id strings and
            # converts them to [{"file_id": ...}] internally.
            assert inputs["file_list"] == ["file-1"]
            if fail_share:
                return ToolResult(False, error="mock share failure")
            return ToolResult(True, {"short_url": "https://share.weiyun.com/abc"})

    # NOTE: the production tool names are underscore-based (weiyun_upload /
    # weiyun_share_link), registered by tools/publishers/* — not the legacy
    # dot-named weiyun.gen_share_link wrapper.
    tools = {
        "video_compose": FakeCompose(),
        "weiyun_upload": FakeUpload(),
        "weiyun_share_link": FakeShare(),
    }
    monkeypatch.setattr(mcp_server.registry, "get", lambda name: tools.get(name))
    return mcp_server


def _capture_progress(monkeypatch, mcp_server):
    events = []
    monkeypatch.setattr(mcp_server, "publish", lambda job_id, event: events.append(event))
    return events


def _prepare_workflow(tmp_path):
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    sessions.register_image("workflow", "demo", {"id": "img-2", "path": str(_image(tmp_path, "two.jpg")), "type": "image", "sha256": "y"})


async def _poll_until(mcp_server, job_id, timeout=30.0):
    """Poll get_render_status until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = mcp_server.get_render_status(job_id)
        if status.get("status") in ("published", "failed"):
            return status
        time.sleep(0.05)
    return mcp_server.get_render_status(job_id)


def test_batch_isolated_and_new_batch_after_publication(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    first = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    state_a = sessions.register_image("session-A", "demo", first)
    state_b = sessions.register_image("session-B", "demo", {**first, "id": "b", "sha256": "b"})
    assert state_a["batch_id"] != state_b["batch_id"]
    assert len(state_a["assets"]) == len(state_b["assets"]) == 1
    sessions.update("session-A", status="published", share_url="https://share.example/a")
    next_state = sessions.register_image("session-A", "demo", {**first, "id": "c", "sha256": "c"})
    assert next_state["batch_id"] != state_a["batch_id"]
    assert next_state["status"] == "collecting_assets"


def test_begin_render_claims_batch_and_rejects_duplicate(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    asset = {"id": "a", "path": str(_image(tmp_path)), "type": "image", "sha256": "a"}
    sessions.register_image("same", "demo", asset)
    _, state = sessions.begin_render("same")
    assert state["status"] == "rendering"
    with pytest.raises(ValueError, match="already being generated"):
        sessions.begin_render("same")


def test_create_share_dispatches_async_and_polls_to_published(monkeypatch, tmp_path):
    """create_remotion_video_share must return immediately (queued) and the
    background job must reach 'published' with a share URL and no error."""
    _state_env(monkeypatch, tmp_path)
    _image(tmp_path)
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    sessions.register_image("workflow", "demo", {"id": "img-2", "path": str(_image(tmp_path, "two.jpg")), "type": "image", "sha256": "y"})
    mcp_server = _install_fakes(monkeypatch, tmp_path)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        # Non-blocking contract: queued + job id, nothing rendered yet.
        assert result["success"] is True
        assert result["status"] == "queued"
        job_id = result["render_job_id"]
        assert job_id
        final = asyncio.run(_poll_until(mcp_server, job_id))
    finally:
        reset_mcp_session_id(token)

    assert final["success"] is True
    assert final["status"] == "published"
    assert final["stage"] is None
    assert final["error"] is None
    assert final["video_path"].endswith(".mp4")
    assert final["share_url"] == "https://share.weiyun.com/abc"


def test_create_share_polls_to_failed_weiyun_share_and_keeps_video(monkeypatch, tmp_path):
    """A share-link failure must surface as a pollable failed state that keeps
    the rendered video path and reports the failure reason."""
    _state_env(monkeypatch, tmp_path)
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    sessions.register_image("workflow", "demo", {"id": "img-2", "path": str(_image(tmp_path, "two.jpg")), "type": "image", "sha256": "y"})
    mcp_server = _install_fakes(monkeypatch, tmp_path, fail_share=True)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        assert result["status"] == "queued"
        job_id = result["render_job_id"]
        final = asyncio.run(_poll_until(mcp_server, job_id))
    finally:
        reset_mcp_session_id(token)

    assert final["status"] == "failed"
    assert final["stage"] == "weiyun_share"
    assert final["error"] == "mock share failure"
    assert final["video_path"].endswith(".mp4")
    state_path = sessions.STATE_DIR / f"{sessions.session_hash('workflow')}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["video_path"] == final["video_path"]


def test_weiyun_share_failure_publishes_failed_sse(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    _prepare_workflow(tmp_path)
    mcp_server = _install_fakes(monkeypatch, tmp_path, fail_share=True)
    events = _capture_progress(monkeypatch, mcp_server)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        final = asyncio.run(_poll_until(mcp_server, result["render_job_id"]))
    finally:
        reset_mcp_session_id(token)
    assert final["stage"] == "weiyun_share"
    assert any(e.get("phase") == "share" and e.get("status") == "failed" for e in events)


def test_create_share_polls_to_failed_weiyun_upload(monkeypatch, tmp_path):
    """An upload failure must report stage=weiyun_upload with its reason."""
    _state_env(monkeypatch, tmp_path)
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    sessions.register_image("workflow", "demo", {"id": "img-2", "path": str(_image(tmp_path, "two.jpg")), "type": "image", "sha256": "y"})
    mcp_server = _install_fakes(monkeypatch, tmp_path, fail_upload=True)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        job_id = result["render_job_id"]
        final = asyncio.run(_poll_until(mcp_server, job_id))
    finally:
        reset_mcp_session_id(token)

    assert final["status"] == "failed"
    assert final["stage"] == "weiyun_upload"
    assert final["error"] == "mock upload failure"


def test_weiyun_upload_failure_publishes_failed_sse(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    _prepare_workflow(tmp_path)
    mcp_server = _install_fakes(monkeypatch, tmp_path, fail_upload=True)
    events = _capture_progress(monkeypatch, mcp_server)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        final = asyncio.run(_poll_until(mcp_server, result["render_job_id"]))
    finally:
        reset_mcp_session_id(token)
    assert final["stage"] == "weiyun_upload"
    assert any(e.get("phase") == "upload" and e.get("status") == "failed" for e in events)


@pytest.mark.parametrize("failure", ["missing_upload", "missing_share", "empty_file_id"])
def test_weiyun_publish_guard_failures_publish_failed_sse(monkeypatch, tmp_path, failure):
    _state_env(monkeypatch, tmp_path)
    _prepare_workflow(tmp_path)
    mcp_server = _install_fakes(monkeypatch, tmp_path)
    original_get = mcp_server.registry.get
    if failure == "missing_upload":
        monkeypatch.setattr(mcp_server.registry, "get", lambda name: None if name == "weiyun_upload" else original_get(name))
    elif failure == "missing_share":
        monkeypatch.setattr(mcp_server.registry, "get", lambda name: None if name == "weiyun_share_link" else original_get(name))
    else:
        class EmptyUpload:
            def execute(self, inputs):
                return ToolResult(True, {"file_id": ""})
        monkeypatch.setattr(mcp_server.registry, "get", lambda name: EmptyUpload() if name == "weiyun_upload" else original_get(name))
    events = _capture_progress(monkeypatch, mcp_server)
    token = set_mcp_session_id("workflow")
    try:
        result = asyncio.run(mcp_server.create_remotion_video_share())
        final = asyncio.run(_poll_until(mcp_server, result["render_job_id"]))
    finally:
        reset_mcp_session_id(token)
    assert final["status"] == "failed"
    assert final["stage"] == ("weiyun_upload" if failure == "missing_upload" else "weiyun_share")
    assert any(e.get("status") == "failed" for e in events)


def test_get_render_status_unknown_job(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server

    status = mcp_server.get_render_status("does-not-exist")
    assert status["success"] is False
    assert "render job" in status["error"]


def _prepare_published_job(tmp_path):
    video = tmp_path / "projects" / "demo" / "renders" / "job.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    _, state = sessions.begin_render("workflow")
    sessions.update("workflow", status="failed", failure_stage="weiyun_upload", video_path=str(video), error="old")
    return state["render_job_id"], video


def test_retry_render_publish_reuses_existing_share_url(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server
    job_id, video = _prepare_published_job(tmp_path)
    sessions.update("workflow", status="published", video_path=str(video), share_url="https://share.weiyun.com/already")
    result = asyncio.run(mcp_server.retry_render_publish(job_id))
    assert result == {"success": True, "render_job_id": job_id, "status": "published", "stage": None, "share_url": "https://share.weiyun.com/already", "error": None}


def test_retry_render_publish_uploads_and_shares_without_render(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server
    job_id, video = _prepare_published_job(tmp_path)
    calls = []

    class FakeUpload:
        def execute(self, inputs):
            calls.append(("upload", inputs))
            return ToolResult(True, {"file_id": "retry-file"})

    class FakeShare:
        def execute(self, inputs):
            calls.append(("share", inputs))
            return ToolResult(True, {"short_url": "https://share.weiyun.com/retried"})

    monkeypatch.setattr(mcp_server.registry, "get", lambda name: {"weiyun_upload": FakeUpload(), "weiyun_share_link": FakeShare()}.get(name))
    result = asyncio.run(mcp_server.retry_render_publish(job_id))
    assert result["success"] is True
    assert result["share_url"] == "https://share.weiyun.com/retried"
    assert [kind for kind, _ in calls] == ["upload", "share"]
    final = mcp_server.get_render_status(job_id)
    assert final["status"] == "published"
    assert final["video_path"] == str(video)


def test_retry_render_publish_failure_keeps_video_path(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server
    job_id, video = _prepare_published_job(tmp_path)

    class FakeUpload:
        def execute(self, inputs):
            return ToolResult(False, error="mock retry upload failure")

    monkeypatch.setattr(mcp_server.registry, "get", lambda name: FakeUpload() if name == "weiyun_upload" else None)
    result = asyncio.run(mcp_server.retry_render_publish(job_id))
    assert result["success"] is False
    assert result["stage"] == "weiyun_upload"
    assert result["error"] == "mock retry upload failure"
    final = mcp_server.get_render_status(job_id)
    assert final["video_path"] == str(video)
    assert final["status"] == "failed"


def test_retry_render_publish_serializes_same_job(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server
    job_id, _ = _prepare_published_job(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = []

    class FakeUpload:
        def execute(self, inputs):
            calls.append("upload")
            started.set()
            release.wait(timeout=5)
            return ToolResult(True, {"file_id": "one-file"})

    class FakeShare:
        def execute(self, inputs):
            calls.append("share")
            return ToolResult(True, {"short_url": "https://share.weiyun.com/serialized"})

    monkeypatch.setattr(mcp_server.registry, "get", lambda name: {"weiyun_upload": FakeUpload(), "weiyun_share_link": FakeShare()}.get(name))

    async def run_both():
        first = asyncio.create_task(mcp_server.retry_render_publish(job_id))
        await asyncio.to_thread(started.wait, 5)
        second = await mcp_server.retry_render_publish(job_id)
        release.set()
        return await first, second

    first, second = asyncio.run(run_both())
    assert first["success"] is True
    assert second["success"] is False
    assert second["stage"] == "in_progress"
    assert calls == ["upload", "share"]


def test_retry_render_publish_rejects_missing_video(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import mcp_server
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(_image(tmp_path)), "type": "image", "sha256": "x"})
    _, state = sessions.begin_render("workflow")
    sessions.update("workflow", status="failed", video_path=str(tmp_path / "missing.mp4"), error="old")
    result = asyncio.run(mcp_server.retry_render_publish(state["render_job_id"]))
    assert result["success"] is False
    assert result["stage"] == "validation"
