import pytest

import lib.workbuddy_session as sessions
from tools.asset_upload import UploadAsset
from tools.asset_upload_chunk import UploadAssetChunk


def _state_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)


def test_register_find_and_list_assets_by_session_project_and_type(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    image = {"id": "image-1", "type": "image", "path": "image.jpg", "sha256": "img"}
    video = {"id": "video-1", "type": "video", "path": "video.mp4", "sha256": "vid"}
    audio = {"id": "audio-1", "type": "audio", "path": "audio.wav", "sha256": "aud"}

    sessions.register_asset("session", "project", image)
    sessions.register_asset("session", "project", video)
    sessions.register_asset("session", "project", audio)

    assert sessions.find_asset("session", "video-1", "project") == video
    assert sessions.find_asset("session", "video-1", "other") is None
    assert sessions.find_asset("session", "missing", "project") is None
    assert sessions.list_assets("session", "project") == [image, video, audio]
    assert sessions.list_assets("session", "project", "audio") == [audio]
    assert sessions.list_assets("session", "other") == []


def test_register_asset_deduplicates_and_preserves_image_batch(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    image = {"id": "image-1", "type": "image", "path": "same.jpg", "sha256": "same"}
    sessions.register_image("session", "project", image)
    state = sessions.register_asset("session", "project", {**image, "id": "image-2"})

    assert len(state["assets"]) == 1
    assert state["assets"][0]["id"] == "image-1"
    assert state["status"] == "collecting_assets"


def test_upload_asset_registers_video_and_audio(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import base64

    tool = UploadAsset()
    monkeypatch.setattr(tool, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(tool, "_project_dir", lambda project_id: tmp_path / "projects" / project_id)
    for filename, mime_type, content in (
        ("clip.mp4", "video/mp4", b"video"),
        ("voice.wav", "audio/wav", b"audio"),
    ):
        result = tool.execute({
            "project_id": "project",
            "filename": filename,
            "content_base64": base64.b64encode(content).decode(),
            "mime_type": mime_type,
            "mcp_session_id": "session",
        })
        assert result.success is True

    assert [asset["type"] for asset in sessions.list_assets("session", "project")] == ["video", "audio"]


def test_chunk_upload_registers_video(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    import base64
    import hashlib

    tool = UploadAssetChunk()
    monkeypatch.setattr(tool, "_root", staticmethod(lambda: (tmp_path / "projects").resolve()))
    content = b"chunked-video"
    start = tool.execute({
        "operation": "start",
        "project_id": "project",
        "filename": "clip.mp4",
        "total_bytes": len(content),
        "mime_type": "video/mp4",
        "sha256": hashlib.sha256(content).hexdigest(),
        "mcp_session_id": "session",
    })
    assert start.success is True
    upload_id = start.data["upload_id"]
    assert tool.execute({
        "operation": "append",
        "upload_id": upload_id,
        "offset": 0,
        "chunk_base64": base64.b64encode(content).decode(),
        "mcp_session_id": "session",
    }).success is True
    complete = tool.execute({
        "operation": "complete",
        "upload_id": upload_id,
        "mcp_session_id": "session",
    })

    assert complete.success is True
    assert sessions.find_asset("session", complete.data["asset"]["id"], "project")["type"] == "video"


def test_asset_registration_requires_session(monkeypatch, tmp_path):
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Mcp-Session-Id"):
        sessions.register_asset(None, "project", {"id": "a", "type": "audio"})
