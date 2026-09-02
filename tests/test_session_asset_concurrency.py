"""Concurrency tests for the cross-process flock in workbuddy_session and the
dedup-race guard in upload_asset_chunk.

Regression tests for the create-video 404 bug: when two processes upload
content with the same sha256 (or the canonical file is missing on disk),
the metadata used to drift from the actual file system. The new flock and
dedup-promotion guard keep the in-memory asset list and the on-disk file
in sync.
"""

import hashlib
import threading

import pytest

from lib import paths as lib_paths
import lib.workbuddy_session as sessions
from lib.principal_registry import Principal
from tools.asset_upload_chunk import UploadAssetChunk


def _state_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)
    projects = (tmp_path / "projects").resolve()
    projects.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib_paths, "PROJECTS_DIR", projects)
    # Phase C removed the tool-local _root() hook. Exercise the production
    # ProjectWorkspace path with a fixed authenticated principal instead.
    import mcp_server
    principal = Principal(kind="user", principal_id="concurrency-tester")
    monkeypatch.setattr(mcp_server, "current_principal", lambda: principal)


def test_register_image_under_flock_serializes_concurrent_writes(monkeypatch, tmp_path):
    """Two threads hitting register_image for the same digest must produce a
    single assets entry (not duplicate appends). The flock + RLock pair must
    hold across threads even though threads share the same process."""
    _state_env(monkeypatch, tmp_path)
    sid = "concurrency-sid"
    digest = hashlib.sha256(b"hello").hexdigest()
    asset_a = {
        "relative_path": "projects/p/assets/_sessions/dummy/a.png",
        "filename": "a.png",
        "sha256": digest,
    }
    asset_b = {
        "relative_path": "projects/p/assets/_sessions/dummy/b.png",
        "filename": "b.png",
        "sha256": digest,
    }

    def worker(asset):
        sessions.register_image(sid, "p", asset)

    threads = [threading.Thread(target=worker, args=(a,)) for a in (asset_a, asset_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = sessions._read(sessions.session_hash(sid))
    assert state is not None
    # sha-dedup at register_image means only one entry is kept.
    assert len(state["assets"]) == 1, state["assets"]


def test_register_image_creates_lockfile(monkeypatch, tmp_path):
    """The cross-process lock file must exist after a register call so that
    a second MCP worker process can find and flock it."""
    _state_env(monkeypatch, tmp_path)
    sid = "lockfile-sid"
    sessions.register_image(
        sid,
        "p",
        {
            "relative_path": "projects/p/assets/_sessions/dummy/x.png",
            "filename": "x.png",
            "sha256": hashlib.sha256(b"x").hexdigest(),
        },
    )
    digest = sessions.session_hash(sid)
    lock_path = sessions._lock_dir() / f"{digest}.lock"
    assert lock_path.exists(), f"expected lock file at {lock_path}"


def test_register_image_rejects_project_id_switch(monkeypatch, tmp_path):
    """A session is bound to one project_id; a second register under a
    different project_id must raise. This is the contract the chunk dedup
    path relies on."""
    _state_env(monkeypatch, tmp_path)
    sid = "switch-sid"
    sessions.register_image(
        sid,
        "proj-A",
        {
            "relative_path": "projects/proj-A/a.png",
            "filename": "a.png",
            "sha256": hashlib.sha256(b"a").hexdigest(),
        },
    )
    with pytest.raises(ValueError, match="another project"):
        sessions.register_image(
            sid,
            "proj-B",
            {
                "relative_path": "projects/proj-B/b.png",
                "filename": "b.png",
                "sha256": hashlib.sha256(b"b").hexdigest(),
            },
        )


def test_register_image_rejects_rendering_session(monkeypatch, tmp_path):
    """Uploads during the rendering window are blocked at register_image,
    which is why the chunk dedup path can rely on a stable state shape."""
    _state_env(monkeypatch, tmp_path)
    sid = "render-sid"
    digest = sessions.session_hash(sid)
    state_path = sessions._state_path(digest)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"project_id":"p","batch_id":"b","status":"rendering","assets":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="currently rendering"):
        sessions.register_image(
            sid,
            "p",
            {
                "relative_path": "projects/p/a.png",
                "filename": "a.png",
                "sha256": hashlib.sha256(b"a").hexdigest(),
            },
        )


def test_chunk_dedup_promotes_self_when_canonical_missing(monkeypatch, tmp_path):
    """If two uploads share a sha256 but the canonical file has been removed
    from disk, the new upload must promote its own file to the canonical
    position rather than silently delete it. This is the regression test
    for the create-video 404 path."""
    _state_env(monkeypatch, tmp_path)

    from lib.mcp_session import set_mcp_session_id

    sid = "dedup-sid"
    set_mcp_session_id(sid)

    project_id = "dedup-proj"
    digest = hashlib.sha256(b"shared-content").hexdigest()

    # Pre-register an asset at canonical path; DO NOT create the file on disk.
    sessions.register_image(
        sid,
        project_id,
        {
            "relative_path": f"projects/{project_id}/assets/_sessions/canon.png",
            "filename": "canon.png",
            "sha256": digest,
        },
    )

    # Now upload the same content under a different filename. The chunk tool
    # should detect the missing canonical file and promote the new upload.
    tool = UploadAssetChunk()
    result = tool.execute(
        {
            "operation": "start",
            "project_id": project_id,
            "filename": "fresh.png",
            "total_bytes": len(b"shared-content"),
            "mime_type": "image/png",
            "sha256": digest,
            "mcp_session_id": sid,
        }
    )
    assert result.success, result.error
    upload_id = result.data["upload_id"]

    append_result = tool.execute(
        {
            "operation": "append",
            "upload_id": upload_id,
            "offset": 0,
            "chunk_base64": __import__("base64").b64encode(b"shared-content").decode(),
            "mcp_session_id": sid,
        }
    )
    assert append_result.success, append_result.error

    complete_result = tool.execute(
        {"operation": "complete", "upload_id": upload_id, "mcp_session_id": sid}
    )
    assert complete_result.success, complete_result.error

    state = sessions._read(sessions.session_hash(sid))
    paths = sorted(a["relative_path"] for a in state["assets"])
    # The promoted asset MUST end up in the assets list under its own path,
    # so the SPA never sees a missing file behind an in-list relative_path.
    assert any("fresh.png" in p for p in paths), state["assets"]
    # And the file must exist on disk.
    promoted = next(a for a in state["assets"] if "fresh.png" in a["relative_path"])
    promoted_abs = tmp_path / promoted["relative_path"]
    assert promoted_abs.exists(), f"promoted file missing: {promoted_abs}"
