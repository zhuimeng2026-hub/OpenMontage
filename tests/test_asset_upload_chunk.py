from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from lib import workbuddy_session
from tools.asset_upload_chunk import UploadAssetChunk


def test_chunk_upload_round_trip_is_session_scoped(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)

    tool = UploadAssetChunk()
    content = b"\x89PNG\r\n\x1a\n" + b"frameflow-test" * 32
    digest = hashlib.sha256(content).hexdigest()
    common = {"mcp_session_id": "test-session"}

    started = tool.execute({
        **common,
        "operation": "start",
        "project_id": "chunk-test",
        "filename": "asset.png",
        "total_bytes": len(content),
        "mime_type": "image/png",
        "sha256": digest,
    })
    assert started.success
    upload_id = started.data["upload_id"]

    midpoint = len(content) // 2
    for offset, piece in ((0, content[:midpoint]), (midpoint, content[midpoint:])):
        appended = tool.execute({
            **common,
            "operation": "append",
            "upload_id": upload_id,
            "offset": offset,
            "chunk_base64": base64.b64encode(piece).decode("ascii"),
        })
        assert appended.success

    completed = tool.execute({**common, "operation": "complete", "upload_id": upload_id})
    assert completed.success
    asset = completed.data["asset"]
    assert asset["sha256"] == digest
    assert (projects.parent / asset["relative_path"]).read_bytes() == content
    assert completed.data["batch"]["status"] == "collecting_assets"


def test_chunk_upload_rejects_different_session(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))

    tool = UploadAssetChunk()
    started = tool.execute({
        "operation": "start",
        "project_id": "chunk-test",
        "filename": "asset.png",
        "total_bytes": 4,
        "mcp_session_id": "owner-session",
    })
    assert started.success

    result = tool.execute({
        "operation": "append",
        "upload_id": started.data["upload_id"],
        "offset": 0,
        "chunk_base64": base64.b64encode(b"data").decode("ascii"),
        "mcp_session_id": "other-session",
    })
    assert not result.success
    assert "different MCP session" in result.error


def _complete_image(tool: UploadAssetChunk, common: dict, content: bytes, filename: str = "asset.png"):
    started = tool.execute({
        **common, "operation": "start", "project_id": "chunk-test", "filename": filename,
        "total_bytes": len(content), "mime_type": "image/png",
        "sha256": hashlib.sha256(content).hexdigest(),
    })
    assert started.success
    upload_id = started.data["upload_id"]
    appended = tool.execute({
        **common, "operation": "append", "upload_id": upload_id, "offset": 0,
        "chunk_base64": base64.b64encode(content).decode("ascii"),
    })
    assert appended.success
    return tool.execute({**common, "operation": "complete", "upload_id": upload_id})


def test_chunk_upload_same_content_same_name_is_idempotent(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAssetChunk()
    common = {"mcp_session_id": "idempotent-session"}
    content = b"same-image-content"

    first = _complete_image(tool, common, content)
    second = _complete_image(tool, common, content)

    assert first.success and first.data["deduplicated"] is False
    assert second.success and second.data["deduplicated"] is True
    assert len(second.data["batch"]["assets"]) == 1
    assert not (projects / "chunk-test" / "assets" / "_sessions" / workbuddy_session.session_hash(common["mcp_session_id"]) / "renamed.png").exists()
    canonical_path = projects.parent / second.data["asset"]["relative_path"]
    assert canonical_path.exists()
    assert second.data["asset"]["relative_path"] == first.data["asset"]["relative_path"]


def test_chunk_upload_same_name_different_content_still_fails(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAssetChunk()
    common = {"mcp_session_id": "collision-session"}
    assert _complete_image(tool, common, b"first-image").success
    result = _complete_image(tool, common, b"different-image")
    assert not result.success
    assert "asset already exists" in result.error


def test_chunk_upload_same_content_different_name_is_batch_deduplicated(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAssetChunk()
    common = {"mcp_session_id": "renamed-dedup-session"}
    content = b"same-image-under-a-new-name"

    first = _complete_image(tool, common, content, "first.png")
    second = _complete_image(tool, common, content, "renamed.png")

    assert first.success and first.data["deduplicated"] is False
    assert second.success and second.data["deduplicated"] is True
    assert len(second.data["batch"]["assets"]) == 1


@pytest.mark.parametrize(
    ("original_filename", "expected_renamed"),
    [
        ("商品主图.png", True),
        ("photo (1)!.jpg", True),
        ("safe-name_01.webp", False),
    ],
)
def test_chunk_upload_sanitizes_filename_and_preserves_extension(
    tmp_path: Path, monkeypatch, original_filename: str, expected_renamed: bool
):
    projects = tmp_path / "projects"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(UploadAssetChunk, "_root", staticmethod(lambda: projects))
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)

    tool = UploadAssetChunk()
    content = b"image-data"
    common = {"mcp_session_id": "sanitize-session"}
    started = tool.execute({
        **common,
        "operation": "start",
        "project_id": "chunk-test",
        "filename": original_filename,
        "total_bytes": len(content),
        "mime_type": "image/png" if original_filename.endswith(".png") else "image/jpeg",
    })

    assert started.success
    safe_filename = started.data["safe_filename"]
    assert started.data["filename"] == safe_filename
    assert started.data["original_filename"] == original_filename
    assert started.data["renamed"] is expected_renamed
    assert Path(safe_filename).suffix == Path(original_filename).suffix.lower()
    upload_id = started.data["upload_id"]

    appended = tool.execute({
        **common,
        "operation": "append",
        "upload_id": upload_id,
        "offset": 0,
        "chunk_base64": base64.b64encode(content).decode("ascii"),
    })
    assert appended.success
    completed = tool.execute({**common, "operation": "complete", "upload_id": upload_id})
    assert completed.success
    asset = completed.data["asset"]
    assert asset["filename"] == safe_filename
    assert asset["original_filename"] == original_filename
    assert Path(asset["relative_path"]).name == safe_filename
    assert (projects.parent / asset["relative_path"]).read_bytes() == content
