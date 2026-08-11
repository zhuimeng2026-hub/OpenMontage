import json
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


def test_create_share_builds_remotion_plan_and_keeps_video_on_share_failure(monkeypatch, tmp_path):
    import mcp_server

    _state_env(monkeypatch, tmp_path)
    image = _image(tmp_path)
    sessions.register_image("workflow", "demo", {"id": "img-1", "path": str(image), "type": "image", "sha256": "x"})
    second = _image(tmp_path, "two.jpg")
    sessions.register_image("workflow", "demo", {"id": "img-2", "path": str(second), "type": "image", "sha256": "y"})
    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)
    token = set_mcp_session_id("workflow")

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
            return ToolResult(True, {"file_id": "file-1"})

    class FakeShare:
        def execute(self, inputs):
            assert inputs["file_list"] == [{"file_id": "file-1"}]
            return ToolResult(False, error="mock share failure")

    tools = {"video_compose": FakeCompose(), "weiyun_upload": FakeUpload(), "weiyun.gen_share_link": FakeShare()}
    monkeypatch.setattr(mcp_server.registry, "get", lambda name: tools.get(name))
    try:
        result = mcp_server.create_remotion_video_share()
    finally:
        reset_mcp_session_id(token)
    assert result["success"] is False
    assert result["stage"] == "weiyun_share"
    assert result["video_path"].endswith(".mp4")
    state_path = sessions.STATE_DIR / f"{sessions.session_hash('workflow')}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["video_path"] == result["video_path"]
