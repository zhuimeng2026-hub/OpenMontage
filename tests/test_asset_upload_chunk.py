from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from lib import paths as lib_paths
from lib import workbuddy_session
from lib.principal_registry import Principal
from tools.asset_upload_chunk import UploadAssetChunk


# Phase C: ProjectWorkspace reads ``lib.paths.PROJECTS_DIR`` at call time
# (rather than caching it at import), so test fixtures monkeypatch the
# shared module constant. ``_root`` no longer exists on the tool — the
# workspace factory is the single source of truth for path computation.
#
# ``ProjectWorkspace.for_current_principal`` calls
# ``mcp_server.current_principal()``, which in turn consults a ContextVar
# then the durable registry. Tests stub ``mcp_server.current_principal``
# to return a fixed Principal so the chunked-upload tests stay
# self-contained — no FastMCP server, no ASGI middleware required.


def _stub_current_principal(monkeypatch, principal: Principal) -> None:
    """Patch ``mcp_server.current_principal`` so the tool's workspace
    factory sees a known principal without touching real session state.

    Equivalent to setting the ``_user_id_ctx`` ContextVar in
    mcp_server + populating the registry, but at a higher level so the
    stub doesn't leak between tests.
    """
    import mcp_server
    monkeypatch.setattr(mcp_server, "current_principal", lambda: principal)


def _projects_root(monkeypatch, tmp_path: Path) -> Path:
    """Point ``lib.paths.PROJECTS_DIR`` at ``tmp_path/projects`` so every
    write from the chunked-upload tool lands under the test's tmp tree.
    Returns the resolved projects root so callers can build assertions.
    """
    projects = (tmp_path / "projects").resolve()
    projects.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib_paths, "PROJECTS_DIR", projects)
    return projects


def _default_principal() -> Principal:
    """Return the fixed Principal the tests run as.

    Using a real ``Principal`` (not a Mock) keeps ``namespace_key`` and
    the kind/user distinction exercised by the production code path.
    """
    return Principal(kind="user", principal_id="chunk-tester")


def test_chunk_upload_round_trip_is_session_scoped(tmp_path: Path, monkeypatch):
    projects = _projects_root(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    _stub_current_principal(monkeypatch, _default_principal())
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
    # Phase C: asset lands under ``projects/users/<ns>/<project_id>/assets/``.
    # Resolved via ``projects.parent`` (the tmp_path) so the assertion is
    # agnostic to the namespace_key the registry picked for the principal.
    assert (projects.parent / asset["relative_path"]).read_bytes() == content
    assert completed.data["batch"]["status"] == "collecting_assets"


def test_chunk_complete_migrates_safe_legacy_global_state(tmp_path: Path, monkeypatch):
    """An in-flight v1 upload migrates only after session ownership checks."""
    projects = _projects_root(monkeypatch, tmp_path)
    _stub_current_principal(monkeypatch, _default_principal())
    session_id = "legacy-session"
    digest = workbuddy_session.session_hash(session_id)
    upload_id = "a" * 32
    legacy = projects / ".uploads"
    legacy.mkdir(parents=True)
    content = b"legacy-data"
    (legacy / f"{upload_id}.part").write_bytes(content)
    (legacy / f"{upload_id}.json").write_text(json.dumps({
        "project_id": "chunk-test", "filename": "asset.png",
        "total_bytes": len(content), "mime_type": "image/png",
        "sha256": hashlib.sha256(content).hexdigest(), "session_hash": digest,
    }), encoding="utf-8")

    result = UploadAssetChunk().execute({"operation": "complete", "upload_id": upload_id, "mcp_session_id": session_id})

    assert result.success, result.error
    assert not (legacy / f"{upload_id}.json").exists()
    assert not (legacy / f"{upload_id}.part").exists()
    assert (projects.parent / result.data["asset"]["relative_path"]).read_bytes() == content


def test_chunk_legacy_global_state_wrong_session_fails_closed(tmp_path: Path, monkeypatch):
    projects = _projects_root(monkeypatch, tmp_path)
    _stub_current_principal(monkeypatch, _default_principal())
    upload_id = "b" * 32
    legacy = projects / ".uploads"
    legacy.mkdir(parents=True)
    (legacy / f"{upload_id}.part").write_bytes(b"data")
    (legacy / f"{upload_id}.json").write_text(json.dumps({
        "project_id": "chunk-test", "filename": "asset.png",
        "total_bytes": 4, "session_hash": workbuddy_session.session_hash("owner"),
    }), encoding="utf-8")

    result = UploadAssetChunk().execute({"operation": "complete", "upload_id": upload_id, "mcp_session_id": "attacker"})

    assert not result.success
    assert "not found or expired" in result.error
    assert (legacy / f"{upload_id}.json").exists()


def test_chunk_upload_rejects_different_session(tmp_path: Path, monkeypatch):
    _projects_root(monkeypatch, tmp_path)
    _stub_current_principal(monkeypatch, _default_principal())

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
    projects = _projects_root(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    _stub_current_principal(monkeypatch, _default_principal())
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAssetChunk()
    common = {"mcp_session_id": "idempotent-session"}
    content = b"same-image-content"

    first = _complete_image(tool, common, content)
    second = _complete_image(tool, common, content)

    assert first.success and first.data["deduplicated"] is False
    assert second.success and second.data["deduplicated"] is True
    assert len(second.data["batch"]["assets"]) == 1
    # Pre-Phase-C layout: ``projects/<id>/assets/_sessions/<digest>/<name>``
    # no longer exists (Phase C moved files under
    # ``projects/users/<ns>/<id>/assets/...``); assert the legacy path is
    # empty so we know the test is exercising the new layout, not a stale
    # write through some other code path.
    assert not (projects / "chunk-test" / "assets" / "_sessions" / workbuddy_session.session_hash(common["mcp_session_id"]) / "renamed.png").exists()
    canonical_path = projects.parent / second.data["asset"]["relative_path"]
    assert canonical_path.exists()
    assert second.data["asset"]["relative_path"] == first.data["asset"]["relative_path"]


def test_chunk_upload_same_name_different_content_still_fails(tmp_path: Path, monkeypatch):
    _projects_root(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    _stub_current_principal(monkeypatch, _default_principal())
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAssetChunk()
    common = {"mcp_session_id": "collision-session"}
    assert _complete_image(tool, common, b"first-image").success
    result = _complete_image(tool, common, b"different-image")
    assert not result.success
    assert "asset already exists" in result.error


def test_chunk_upload_same_content_different_name_is_batch_deduplicated(tmp_path: Path, monkeypatch):
    _projects_root(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    _stub_current_principal(monkeypatch, _default_principal())
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
    projects = _projects_root(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    _stub_current_principal(monkeypatch, _default_principal())
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


def _tool_over(tmp_path: Path, monkeypatch) -> UploadAssetChunk:
    """Set up ``PROJECTS_DIR`` + a stub principal so any chunk tool call
    that does not explicitly bind a session gets a known workspace.

    Note: production code calls ``ProjectWorkspace.for_current_principal``
    which raises ``PrincipalNotFound`` if no binding exists. The chunk
    tests that *succeed* need a bound principal — ``_stub_current_principal``
    handles those. Tests that *fail* with a validation error before any
    workspace lookup (e.g. missing-argument checks) only need this stub
    if they want to inspect the rejection path.
    """
    _projects_root(monkeypatch, tmp_path)
    _stub_current_principal(monkeypatch, _default_principal())
    return UploadAssetChunk()


def test_chunk_start_reports_every_missing_argument(tmp_path: Path, monkeypatch):
    """Regression: on 2026-08-30 04:53 a client called start with only
    operation+total_bytes and got a bare "project_id must be a safe basename".
    It must instead be told exactly which arguments are missing, and nothing
    may be written to disk.
    """
    projects = tmp_path / "projects"
    tool = _tool_over(tmp_path, monkeypatch)

    result = tool.execute({
        "operation": "start",
        "total_bytes": 1_486_629,
        "mcp_session_id": "bare-start-session",
    })

    assert not result.success
    assert "missing required argument" in result.error
    assert "project_id" in result.error
    assert "filename" in result.error
    assert not (projects / ".uploads").exists()


@pytest.mark.parametrize(
    "project_id",
    ["my project", "../evil", "/abs/path", "with/slash", "项目A", "-leading", "a" * 129],
)
def test_chunk_start_rejects_unsafe_project_id(
    tmp_path: Path, monkeypatch, project_id: str
):
    projects = tmp_path / "projects"
    tool = _tool_over(tmp_path, monkeypatch)

    result = tool.execute({
        "operation": "start",
        "project_id": project_id,
        "filename": "asset.png",
        "total_bytes": 4,
        "mcp_session_id": "unsafe-project-session",
    })

    assert not result.success
    assert "safe basename" in result.error
    assert not (projects / ".uploads").exists()


def test_chunk_upload_rejects_unknown_operation(tmp_path: Path, monkeypatch):
    projects = tmp_path / "projects"
    tool = _tool_over(tmp_path, monkeypatch)

    result = tool.execute({
        "operation": "bogus",
        "project_id": "chunk-test",
        "mcp_session_id": "bad-operation-session",
    })

    assert not result.success
    assert "operation must be start, append, or complete" in result.error
    assert not (projects / ".uploads").exists()
