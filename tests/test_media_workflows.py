from __future__ import annotations

import asyncio
from pathlib import Path

import lib.media_job_store as media_store
import lib.workbuddy_session as sessions
import mcp_server
from tools.base_tool import ToolResult


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(media_store, "STORE_PATH", tmp_path / "media-jobs.json")
    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "sessions")


def _asset(project: str, path: Path, kind: str, asset_id: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return {
        "id": asset_id, "path": str(path), "type": kind,
        "sha256": asset_id.ljust(64, "0")[:64], "filename": path.name,
    }


def test_caption_job_is_queued_from_registered_video(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project = "caption-job"
    project_root = mcp_server._PROJECT_ROOT / "projects" / project
    video = _asset(project, project_root / "assets" / "input.mp4", "video", "video-1")
    sessions.register_asset("sid-1", project, video)
    monkeypatch.setattr(mcp_server, "get_mcp_session_id", lambda: "sid-1")

    started = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon):
            started.append((target, kwargs, daemon))

        def start(self):
            return None

    monkeypatch.setattr(mcp_server.threading, "Thread", FakeThread)
    result = asyncio.run(mcp_server.create_captioned_video_share(project, "video-1"))

    assert result["success"] is True
    assert result["status"] == "queued"
    assert len(started) == 1
    record = media_store.get_job(result["job_id"])
    assert record["job_type"] == "captioned_video"
    assert record["session_hash"] == sessions.session_hash("sid-1")


def test_cloned_voice_requires_explicit_consent():
    result = asyncio.run(mcp_server.create_cloned_voice_video_share(
        "p", "video", "voice", "hello", voice_consent=False,
    ))
    assert result["success"] is False
    assert result["stage"] == "consent"


def test_media_job_status_is_session_scoped(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    record = media_store.create_job(
        session_hash=sessions.session_hash("owner"), project_id="p", job_type="captioned_video",
    )
    monkeypatch.setattr(mcp_server, "get_mcp_session_id", lambda: "other")
    result = mcp_server.get_render_status(record["job_id"])
    assert result["success"] is False


def test_caption_worker_publishes_share_link(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project = "worker-job"
    project_root = mcp_server._PROJECT_ROOT / "projects" / project
    video = project_root / "assets" / "input.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    record = media_store.create_job(
        session_hash=sessions.session_hash("sid-worker"), project_id=project,
        job_type="captioned_video",
    )

    monkeypatch.setattr(mcp_server.registry, "get", lambda name: name)

    async def fake_run(tool, inputs):
        if tool == "transcriber":
            return ToolResult(True, {"segments": [{"text": "hello", "start": 0, "end": 1, "words": [{"word": "hello", "start": 0, "end": 1}]}]})
        if tool == "remotion_caption_burn":
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"captioned")
            return ToolResult(True, {"output": str(output)}, [str(output)])
        if tool == "weiyun_upload":
            return ToolResult(True, {"file_id": "file-1"})
        if tool == "weiyun_share_link":
            return ToolResult(True, {"share_url": "https://share.example/video"})
        raise AssertionError(tool)

    monkeypatch.setattr(mcp_server, "_run_tool_sync", fake_run)
    mcp_server._run_media_workflow(
        sid="sid-worker", job_id=record["job_id"], project_id=project,
        job_type="captioned_video", video_path=str(video), voice_sample_path=None,
        script=None, language="zh", subtitle=True, subtitle_style="short_video",
        title="Test",
    )

    completed = media_store.get_job(record["job_id"])
    assert completed["status"] == "published"
    assert completed["progress"] == 100
    assert completed["result_url"] == "https://share.example/video"


def test_recover_marks_incomplete_media_jobs_failed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    record = media_store.create_job(session_hash="abc", project_id="p", job_type="captioned_video")
    assert media_store.recover_incomplete_jobs() == 1
    recovered = media_store.get_job(record["job_id"])
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "orphaned"
