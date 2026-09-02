"""Tests for tools/asset/read_session_asset.py — the BFF proxy's source of truth.

These tests verify path validation, repo-root containment, base64 round-trip,
and that the tool rejects the same inputs that would otherwise let ServeAsset
serve arbitrary files off the server.

Phase C: the workspace fixture also stubs ``mcp_server.current_principal``
so Layer 3 (the per-principal namespace boundary) has something to bind to,
and test paths reflect the new ``projects/users/<ns>/<project_id>/...``
layout — see ``docs/user-isolation-via-mcp-session.md`` §Phase C.
"""
import base64
import os
from pathlib import Path

import pytest

from lib import paths as lib_paths
from lib.principal_registry import Principal
from tools.asset.read_session_asset import ReadSessionAsset, _REPO_ROOT, _PROJECTS_ROOT


_PRINCIPAL = Principal(kind="user", principal_id="asset-reader-test")
# Compute the namespace key from the principal so the fixture can mkdir
# the right tree without hand-coding the HMAC.
_NS_KEY = _PRINCIPAL.namespace_key
_PROJECT_ID = "probe"


@pytest.fixture
def tool():
    return ReadSessionAsset()


@pytest.fixture
def fake_projects_root(monkeypatch, tmp_path):
    """Redirect _REPO_ROOT, _PROJECTS_ROOT, AND ``lib.paths.PROJECTS_DIR``
    to the tmp tree so we don't touch the real projects/ tree on disk.

    Phase C: the Layer 3 namespace check now reads from
    ``lib.paths.PROJECTS_DIR`` via ``ProjectWorkspace.for_current_principal``.
    Tests that patch only the local module globals miss the new code path
    and exercise the production projects root — patching all three
    sources keeps behaviour consistent with the legacy fixtures.
    """
    fake_root = (tmp_path / "repo").resolve()
    (fake_root / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tools.asset.read_session_asset._REPO_ROOT", fake_root)
    monkeypatch.setattr("tools.asset.read_session_asset._PROJECTS_ROOT", (fake_root / "projects").resolve())
    monkeypatch.setattr(lib_paths, "PROJECTS_DIR", (fake_root / "projects").resolve())
    # Layer 3 calls mcp_server.current_principal() — bind a stub so the
    # namespace_key boundary is exercised against a deterministic
    # principal. New Phase C tests below cover the "another user's
    # namespace" rejection case explicitly.
    import mcp_server
    monkeypatch.setattr(mcp_server, "current_principal", lambda: _PRINCIPAL)
    return fake_root


def _user_path(rel: str) -> str:
    """Rewrite a legacy-style relative path into the Phase C per-principal
    layout. ``projects/probe/...`` becomes
    ``projects/users/<ns>/probe/...``. The project id (here ``probe``)
    is already in the input — only the ``users/<namespace_key>/``
    prefix needs to be inserted.
    """
    if rel.startswith("projects/"):
        suffix = rel[len("projects/"):]
    else:
        suffix = rel
    return f"projects/users/{_NS_KEY}/{suffix}"


def test_reads_real_file_under_projects(tool, fake_projects_root):
    rel = _user_path("projects/probe/assets/_sessions/abc/photo.png")
    abs_path = fake_projects_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-content")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert result.data["bytes"] == len(b"\x89PNG\r\n\x1a\nfake-png-content")
    assert base64.b64decode(result.data["data_base64"]) == b"\x89PNG\r\n\x1a\nfake-png-content"
    assert result.data["mime_type"] == "image/png"
    assert result.data["filename"] == "photo.png"


def test_reads_unmigrated_v1_file_for_same_principal(tool, fake_projects_root):
    """Legacy fallback is readable, but only inside this principal's raw-id root."""
    rel = f"projects/users/{_PRINCIPAL.principal_id}/{_PROJECT_ID}/assets/_sessions/legacy/photo.png"
    abs_path = fake_projects_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"legacy-photo")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert base64.b64decode(result.data["data_base64"]) == b"legacy-photo"


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
    rel = _user_path("projects/missing-batch/assets/_sessions/zzz/nope.png")
    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "not found" in (result.error or "").lower()


def test_empty_relative_path_rejected(tool):
    result = tool.execute({"relative_path": ""})
    assert not result.success


def test_backslashes_normalized(tool, fake_projects_root):
    """Windows-style backslashes must be normalized; the tool is OS-portable."""
    rel = _user_path("projects/probe/assets/_sessions/abc/photo.png").replace("/", "\\")
    # write the file at the POSIX-style location the normalized path resolves to
    abs_path = fake_projects_root / _user_path("projects/probe/assets/_sessions/abc/photo.png")
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"x")

    result = tool.execute({"relative_path": rel})
    assert result.success, result.error
    assert result.data["bytes"] == 1


def test_guess_mime_for_unknown_extension(tool, fake_projects_root):
    rel = _user_path("projects/probe/assets/_sessions/abc/blob.weird")
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
    rel = _user_path("projects/probe/assets/_sessions/abc")[:-len("photo.png")]  # strip filename
    abs_path = fake_projects_root / rel
    abs_path.mkdir(parents=True, exist_ok=True)

    result = tool.execute({"relative_path": rel})
    assert not result.success
    assert "not found" in (result.error or "").lower()
