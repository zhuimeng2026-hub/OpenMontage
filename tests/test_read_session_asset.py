"""Tests for tools/asset/read_session_asset.py — the BFF proxy's source of truth.

These tests verify path validation, repo-root containment, base64 round-trip,
and that the tool rejects the same inputs that would otherwise let ServeAsset
serve arbitrary files off the server.
"""
import base64
import os
from pathlib import Path

import pytest

from tools.asset.read_session_asset import ReadSessionAsset, _REPO_ROOT, _PROJECTS_ROOT


@pytest.fixture
def tool():
    return ReadSessionAsset()


@pytest.fixture
def fake_projects_root(monkeypatch, tmp_path):
    """Redirect _REPO_ROOT and _PROJECTS_ROOT to tmp_path so we don't touch the
    real projects/ tree on disk."""
    fake_root = (tmp_path / "repo").resolve()
    (fake_root / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tools.asset.read_session_asset._REPO_ROOT", fake_root)
    monkeypatch.setattr("tools.asset.read_session_asset._PROJECTS_ROOT", (fake_root / "projects").resolve())
    return fake_root


def test_reads_real_file_under_projects(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc/photo.png"
    abs_path = fake_projects_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-content")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert result.data["bytes"] == len(b"\x89PNG\r\n\x1a\nfake-png-content")
    assert base64.b64decode(result.data["data_base64"]) == b"\x89PNG\r\n\x1a\nfake-png-content"
    assert result.data["mime_type"] == "image/png"
    assert result.data["filename"] == "photo.png"


def test_rejects_traversal(tool, fake_projects_root):
    for bad in (
        "projects/probe/../../etc/passwd",
        "../etc/passwd",
        "/etc/passwd",
        "projects/probe/./../../escape.txt",
    ):
        result = tool.execute({"relative_path": bad})
        assert not result.success, f"should reject {bad!r}"
        assert "error" in (result.error or "").lower() or "outside" in (result.error or "") or "escapes" in (result.error or "")


def test_rejects_path_outside_projects(tool, fake_projects_root):
    """Paths under the repo but NOT under projects/ must be rejected."""
    bad = "tools/base_tool.py"
    result = tool.execute({"relative_path": bad})
    assert not result.success
    assert "outside" in (result.error or "").lower() or "projects" in (result.error or "").lower()


def test_missing_file_returns_clean_error(tool, fake_projects_root):
    rel = "projects/missing-batch/assets/_sessions/zzz/nope.png"
    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "not found" in (result.error or "").lower()


def test_empty_relative_path_rejected(tool):
    result = tool.execute({"relative_path": ""})
    assert not result.success


def test_backslashes_normalized(tool, fake_projects_root):
    """Windows-style backslashes must be normalized; the tool is OS-portable."""
    rel = "projects\\probe\\assets\\_sessions\\abc\\photo.png"
    abs_path = fake_projects_root / "projects" / "probe" / "assets" / "_sessions" / "abc" / "photo.png"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"x")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert result.data["bytes"] == 1


def test_guess_mime_for_unknown_extension(tool, fake_projects_root):
    rel = "projects/probe/assets/_sessions/abc/blob.weird"
    abs_path = fake_projects_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"\x00\x01\x02")

    result = tool.execute({"relative_path": rel})
    assert result.success
    # Unknown extension falls back to application/octet-stream, never crashes.
    assert result.data["mime_type"] == "application/octet-stream"


def test_does_not_choke_on_directory(tool, fake_projects_root):
    """A directory at the resolved path must be reported as 'not found', not
    raise. The BFF must be able to handle this gracefully for the
    reconciliation path."""
    rel = "projects/probe/assets/_sessions/abc"
    abs_path = fake_projects_root / rel
    abs_path.mkdir(parents=True, exist_ok=True)

    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "not found" in (result.error or "").lower()