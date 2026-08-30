"""Tests for tools/asset/read_session_asset_image.py.

This tool is the first one that returns a real MCP ``Image`` instead of a JSON
dict, so these tests assert on ``ImageContent`` (type/mimeType/base64) rather
than on ``data["data_base64"]`` — including one end-to-end check through
FastMCP's own ``_convert_to_content``, which is the code path that decides
whether a client renders an image or dumps base64 text.
"""
import base64

import pytest
from mcp.server.fastmcp.utilities.types import Image

try:  # mcp >= 1.9 moved the converter into func_metadata; prod venv is 1.29.
    from mcp.server.fastmcp.utilities.func_metadata import _convert_to_content
except ImportError:  # mcp 1.8 keeps it in fastmcp.server.
    from mcp.server.fastmcp.server import _convert_to_content  # type: ignore[no-redef]

from tools.asset import read_session_asset as rsa
from tools.asset.read_session_asset_image import ReadSessionAssetImage, _max_image_bytes

# A real (tiny) 1x1 PNG, so the payload is decodable by an actual image
# renderer rather than just being round-tripped as bytes.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"

    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def tool():
    return ReadSessionAssetImage()


@pytest.fixture
def fake_projects_root(monkeypatch, tmp_path):
    """Redirect the repo root to tmp_path so we never touch the real
    projects/ tree on disk.

    Path validation is delegated to ``ReadSessionAsset._validate_relative``,
    which reads ``_REPO_ROOT`` / ``_PROJECTS_ROOT`` from the
    ``read_session_asset`` module globals — patching them there covers the
    new tool too, and guarantees both tools share one containment rule.
    """
    fake_root = (tmp_path / "repo").resolve()
    (fake_root / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rsa, "_REPO_ROOT", fake_root)
    monkeypatch.setattr(rsa, "_PROJECTS_ROOT", (fake_root / "projects").resolve())
    return fake_root


def _write(fake_root, rel, payload):
    abs_path = fake_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(payload)
    return abs_path


def test_returns_image_content_block_for_png(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc/photo.png"
    _write(fake_projects_root, rel, PNG_BYTES)

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error

    image = result.data["image"]
    assert isinstance(image, Image)

    content = image.to_image_content()
    assert content.type == "image"
    assert content.mimeType == "image/png"
    assert base64.b64decode(content.data) == PNG_BYTES


def test_fastmcp_converts_the_result_to_an_image_block(tool, fake_projects_root):
    """The regression this tool exists for: dict -> TextContent, Image ->
    ImageContent. Go through FastMCP's real conversion function."""
    rel = "projects/probe/assets/_sessions/abc/photo.png"
    _write(fake_projects_root, rel, PNG_BYTES)

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error

    blocks = _convert_to_content(result.data["image"])
    assert len(blocks) == 1
    assert blocks[0].type == "image"
    assert blocks[0].mimeType == "image/png"
    assert base64.b64decode(blocks[0].data) == PNG_BYTES


def test_jpeg_maps_to_image_jpeg(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc/photo.jpg"
    _write(fake_projects_root, rel, b"\xff\xd8\xff\xe0jpeg-payload")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    content = result.data["image"].to_image_content()
    assert content.mimeType == "image/jpeg"


@pytest.mark.parametrize("rel", [
    "projects/probe/assets/_sessions/abc/clip.mp4",
    "projects/probe/assets/_sessions/abc/caption.srt",
    "projects/probe/assets/_sessions/abc/voice.mp3",
    "projects/probe/assets/_sessions/abc/notes.txt",
    "projects/probe/assets/_sessions/abc/noext",
])
def test_rejects_non_image_assets(tool, fake_projects_root, rel):
    """mp4/srt/mp3 are not in mcp's mime table — they would come back as
    application/octet-stream and render as a broken image."""
    _write(fake_projects_root, rel, b"definitely-not-an-image")

    result = tool.execute({"relative_path": rel})
    assert not result.success
    error = (result.error or "").lower()
    assert "unsupported" in error
    assert "png" in error, "error should list the supported formats"


def test_rejects_traversal_outside_repo(tool, fake_projects_root, tmp_path):
    """../../etc/passwd must never resolve outside the fake repo root."""
    outside = (tmp_path / "outside-secret.bin").resolve()
    outside.write_bytes(b"TOP-SECRET")

    for bad in (
        "../../etc/passwd",
        "projects/probe/../../etc/passwd",
        "/etc/passwd",
        "projects/probe/./../../outside-secret.bin",
    ):
        result = tool.execute({"relative_path": bad})
        assert not result.success, f"should reject {bad!r}"
        error = (result.error or "").lower()
        assert "escapes" in error or "outside" in error
        assert "TOP-SECRET" not in error


def test_rejects_path_outside_projects(tool, fake_projects_root):
    """Repo-relative but not under projects/ — same rule as the sibling tool."""
    result = tool.execute({"relative_path": "tools/base_tool.py"})
    assert not result.success
    error = (result.error or "").lower()
    assert "outside" in error or "projects" in error


def test_missing_file_returns_clean_error(tool, fake_projects_root):
    result = tool.execute({"relative_path": "projects/nope/assets/_sessions/zzz/gone.png"})
    assert not result.success
    assert "not found" in (result.error or "").lower()


def test_empty_relative_path_rejected(tool):
    result = tool.execute({"relative_path": ""})
    assert not result.success


def test_rejects_oversized_image(monkeypatch, tool, fake_projects_root):
    """Limit is derived from the shared upload budget, not hardcoded: 1 MB
    envelope -> 786432 raw bytes once base64 inflation is accounted for."""
    monkeypatch.setenv("OPENMONTAGE_MAX_UPLOAD_MB", "1")
    limit = _max_image_bytes()
    assert limit < 1024 * 1024

    rel = "projects/probe/assets/_sessions/abc/huge.png"
    _write(fake_projects_root, rel, b"\x00" * (limit + 1))

    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "too large" in (result.error or "").lower()


def test_accepts_image_at_the_size_limit(monkeypatch, tool, fake_projects_root):
    monkeypatch.setenv("OPENMONTAGE_MAX_UPLOAD_MB", "1")
    rel = "projects/probe/assets/_sessions/abc/exact.png"
    payload = PNG_BYTES + b"\x00" * (_max_image_bytes() - len(PNG_BYTES))
    _write(fake_projects_root, rel, payload)

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert len(base64.b64decode(result.data["image"].to_image_content().data)) == _max_image_bytes()


def test_backslashes_normalized(tool, fake_projects_root):
    rel = "projects\\probe\\assets\\_sessions\\abc\\photo.png"
    _write(fake_projects_root, "projects/probe/assets/_sessions/abc/photo.png", PNG_BYTES)

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert result.data["bytes"] == len(PNG_BYTES)


def test_does_not_choke_on_directory(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc"
    (fake_projects_root / rel).mkdir(parents=True, exist_ok=True)

    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "not found" in (result.error or "").lower()


def test_empty_file_rejected(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc/zero.png"
    _write(fake_projects_root, rel, b"")

    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "empty" in (result.error or "").lower()
