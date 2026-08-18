"""Tests for _resolve_session_asset_path (heterogeneous Win10/Ubuntu path fix).

Session assets are persisted with a posix ``relative_path`` relative to the
repo root. Render-time recomputes the absolute path from ``relative_path`` so a
session uploaded on Windows still resolves on Linux. The upload-time absolute
``path`` is intentionally never used — it is OS-specific and invalid elsewhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# mcp_server is importable without starting the server (server only runs under
# __main__). It does register tools at import time, which is a few seconds.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_server import _PROJECT_ROOT, _resolve_session_asset_path  # noqa: E402


def _make_asset(relative_path, *, make_file=False):
    """Build an asset dict under _PROJECT_ROOT and optionally materialise it."""
    if make_file and relative_path:
        target = (_PROJECT_ROOT / relative_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n-test-bytes")
    return {
        "id": "t-1",
        "relative_path": relative_path,
        "type": "image",
        "mime_type": "image/png",
    }


def test_relative_path_resolves_under_repo_root():
    rel = ".test_assets/resolve/happy.png"
    asset = _make_asset(rel, make_file=True)
    got = _resolve_session_asset_path(asset)
    assert got.is_file()
    assert got == (_PROJECT_ROOT / rel).resolve()


def test_missing_relative_path_raises():
    asset = {"relative_path": None, "type": "image"}
    with pytest.raises(ValueError):
        _resolve_session_asset_path(asset)


def test_windows_absolute_ignored_cross_os():
    # The persisted dict may still carry a Windows absolute path; it must be
    # ignored in favour of the posix relative_path (which resolves on Linux).
    rel = ".test_assets/resolve/cross_os.png"
    asset = _make_asset(rel, make_file=True)
    asset["path"] = "C:\\Users\\Admin\\OpenMontage\\projects\\p\\assets\\_sessions\\deadbeef\\a.png"
    got = _resolve_session_asset_path(asset)
    assert got.is_file()
    # The bogus session hash from the Windows absolute path must NOT leak in;
    # the result must come purely from relative_path under the current root.
    assert got == (_PROJECT_ROOT / rel).resolve()
    assert "deadbeef" not in str(got)


def test_missing_file_raises():
    rel = ".test_assets/resolve/does_not_exist.png"
    asset = _make_asset(rel, make_file=False)
    with pytest.raises(ValueError):
        _resolve_session_asset_path(asset)


def teardown_module(module):
    import shutil
    d = _PROJECT_ROOT / ".test_assets" / "resolve"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
