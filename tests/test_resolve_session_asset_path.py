"""Tests for _resolve_session_asset_path (heterogeneous Win10/Ubuntu path fix).

The upload tools persist each asset with both an OS-specific absolute `path`
(e.g. C:\\Users\\... on Windows) and a posix `relative_path` relative to the
repo root. Render-time must recompute the absolute path from `relative_path`
so a session uploaded on Windows still resolves on Linux.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# mcp_server is importable without starting the server (server only runs under
# __main__). It does register tools at import time, which is a few seconds.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_server import _PROJECT_ROOT, _resolve_session_asset_path  # noqa: E402


def _make_asset(relative_path, absolute_path, *,
                make_file=False, rel_is_posix=True):
    """Build an asset dict under _PROJECT_ROOT and optionally materialise it."""
    if make_file and relative_path:
        target = (_PROJECT_ROOT / relative_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n-test-bytes")
    return {
        "id": "t-1",
        "relative_path": relative_path if rel_is_posix else None,
        "path": absolute_path,
        "type": "image",
        "mime_type": "image/png",
    }


def test_relative_path_resolves_under_repo_root():
    rel = ".test_assets/resolve/win_vs_linux.png"
    asset = _make_asset(rel, "C:\\Users\\nobody\\OpenMontage\\projects\\x\\assets\\y.png",
                        make_file=True)
    got = _resolve_session_asset_path(asset)
    assert got.is_file()
    assert got == (_PROJECT_ROOT / rel).resolve()
    # The broken/absent Windows absolute (note the "nobody" segment) must NOT
    # be returned while the relative path resolves to a real file.
    assert "nobody" not in str(got)


def test_absolute_fallback_when_relative_missing():
    # No relative_path; absolute points to a real file we create.
    target = (_PROJECT_ROOT / ".test_assets/resolve/abs_fallback.png").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"abs-fallback")
    asset = {"relative_path": None, "path": str(target), "type": "image"}
    got = _resolve_session_asset_path(asset)
    assert got == target


def test_relative_preferred_over_broken_absolute():
    # Simulates Win10 upload persisted, now read on Linux: the absolute
    # Windows path cannot exist, but relative_path must still resolve.
    rel = ".test_assets/resolve/cross_os.png"
    asset = _make_asset(rel, "C:\\Users\\Admin\\OpenMontage\\projects\\p\\assets\\_sessions\\deadbeef\\a.png",
                        make_file=True)
    got = _resolve_session_asset_path(asset)
    assert got.is_file()
    assert str(got).startswith(str(_PROJECT_ROOT))


def test_missing_file_returns_preferred_candidate():
    rel = ".test_assets/resolve/does_not_exist.png"
    asset = _make_asset(rel, "C:\\Users\\Admin\\nope.png", make_file=False)
    got = _resolve_session_asset_path(asset)
    # No file exists; function still returns a Path (preferring relative) so
    # the caller can raise a precise "not a readable image" error.
    assert isinstance(got, Path)
    assert not got.is_file()


def teardown_module(module):
    import shutil
    d = _PROJECT_ROOT / ".test_assets" / "resolve"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
