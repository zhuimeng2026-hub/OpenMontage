"""Phase C integration tests for ``lib.project_workspace.ProjectWorkspace``.

These tests cover the three contracts the v2 doc pins on Phase C:

1. **Path layout** matches ``projects/users/<ns>/<project_id>/{assets,
   artifacts,renders,checkpoints}/`` (and ``projects/services/<ns>/...`` for
   service principals) — every sub-path the workspace exposes.
2. **Per-principal disjointness**: two principals land in disjoint
   directories even if they pick the same ``project_id``.
3. **Resolve boundary**: ``resolve(relative)`` accepts only paths under
   ``self.root`` after symlink-aware ``Path.resolve()``. Anything that
   escapes (absolute paths, ``..``, planted symlinks) raises
   ``WorkspaceErrorError``.

Plus the integration scenarios for the six HIGH-risk touchpoints the audit
flagged: chunked upload state per user, asset upload per-user namespace,
``read_session_asset`` rejects another user's namespace, and
``create_remotion_video_share`` (``mcp_server.py:1762``) anchors its root
to the per-principal workspace.

Run:
    python -m pytest tests/integration/test_project_workspace.py -v

The tests do NOT need a live voicebox / MCP server; the autouse session
fixture in ``tests/integration/conftest.py`` is short-circuited via the
``MCP_TEST_SKIP_VOICEBOX_FIXTURES`` env var below.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import types as _types_module

# Set BEFORE pytest's conftest fixtures import anything voicebox-related.
os.environ.setdefault("MCP_TEST_SKIP_VOICEBOX_FIXTURES", "1")

import pytest

# Workaround for Windows: lib.workbuddy_session does ``import fcntl`` at
# module load, which raises ModuleNotFoundError on Windows. The workspace
# tests never invoke any code path that touches fcntl, so a stub is enough.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = _types_module.ModuleType("fcntl")

from lib import paths as lib_paths
from lib import workbuddy_session
from lib.principal_registry import Principal, PrincipalNotFound
from lib.principal_sanitize import sanitize_project_id, MAX_PROJECT_ID_LEN
from lib.project_workspace import (
    ProjectWorkspace,
    WorkspaceErrorError,
)
from tools.asset_upload import UploadAsset
from tools.asset_upload_chunk import UploadAssetChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_current_principal(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    """Patch ``mcp_server.current_principal`` so the workspace factory's
    fast-path sees a known principal without touching real session state.

    Equivalent to setting ``mcp_server._user_id_ctx`` plus binding into the
    registry, but at a higher level so the stub doesn't bleed between
    tests in the same module.
    """
    import mcp_server
    monkeypatch.setattr(mcp_server, "current_principal", lambda: principal)


def _projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> "Path":
    """Point ``lib.paths.PROJECTS_DIR`` at ``tmp_path/projects`` and return
    the resolved root so callers can build cross-checks.
    """
    projects = (tmp_path / "projects").resolve()
    projects.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib_paths, "PROJECTS_DIR", projects)
    return projects


@pytest.fixture
def projects_root(monkeypatch, tmp_path):
    return _projects_root(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# 1. Layout — matches the v2 doc §Phase C spec
# ---------------------------------------------------------------------------


def test_workspace_layout_matches_v2_spec(projects_root):
    """The dataclass exposes every directory the v2 layout pins, rooted
    under the right principal bucket. ``upload_state`` is per-PRINCIPAL
    (not per-project) per the doc's spec."""
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")

    # Project root lives under projects/users/<ns>/<project_id>.
    assert ws.root == projects_root / "users" / p.namespace_key / "demo"
    # Sub-directories are siblings of one another under the project root.
    assert ws.assets == ws.root / "assets"
    assert ws.artifacts == ws.root / "artifacts"
    assert ws.renders == ws.root / "renders"
    assert ws.checkpoints == ws.root / "checkpoints"
    # upload_state is per-principal: ``projects/users/<ns>/.uploads/``,
    # NOT under the project_id.
    assert ws.upload_state == projects_root / "users" / p.namespace_key / ".uploads"
    assert ws.upload_state.parent == ws.root.parent, (
        "upload_state must be a sibling of the project dir, not nested under it"
    )


def test_workspace_read_falls_back_to_v1_but_prefers_v2(projects_root):
    """The read path is v2-first and scoped to this principal's two roots."""
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    legacy_file = ws.candidates[1] / "assets" / "legacy.txt"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text("v1", encoding="utf-8")
    assert ws.resolve_read("assets/legacy.txt") == legacy_file.resolve()

    v2_file = ws.candidates[0] / "assets" / "legacy.txt"
    v2_file.parent.mkdir(parents=True, exist_ok=True)
    v2_file.write_text("v2", encoding="utf-8")
    assert ws.resolve_read("assets/legacy.txt") == v2_file.resolve()


def test_workspace_for_service_principal_lands_under_services(projects_root):
    """Service principals use the ``services/`` bucket per the v2
    doc. Sharing a principal_id with a user must NOT collide — the
    kind discriminator decides the bucket."""
    p_user = Principal(kind="user", principal_id="identical_id")
    p_svc = Principal(kind="service", principal_id="identical_id")
    ws_user = ProjectWorkspace.for_principal(p_user, "demo")
    ws_svc = ProjectWorkspace.for_principal(p_svc, "demo")
    assert "users" in str(ws_user.root)
    assert "services" in str(ws_svc.root)
    # Same secret + same principal_id ⇒ same namespace_key, but the kind
    # bucket still separates them. The two roots are disjoint even when
    # the canonical input is identical.
    assert ws_user.root != ws_svc.root


# ---------------------------------------------------------------------------
# 2. Disjointness — different principals cannot collide
# ---------------------------------------------------------------------------


def test_workspace_for_different_principals_yields_disjoint_paths(projects_root):
    """Two distinct principals must produce disjoint paths even if they
    happen to pick the same project_id. This is the core Phase C invariant
    — a single typo'd project_id across two users must not overwrite
    anyone's assets."""
    p_a = Principal(kind="user", principal_id="alice")
    p_b = Principal(kind="user", principal_id="bob")
    ws_a = ProjectWorkspace.for_principal(p_a, "shared-project")
    ws_b = ProjectWorkspace.for_principal(p_b, "shared-project")
    assert ws_a.root != ws_b.root
    assert ws_a.assets != ws_b.assets
    assert ws_a.renders != ws_b.renders
    # And the upload_state trees are disjoint too — the per-principal
    # scratchpad doesn't bleed across users.
    assert ws_a.upload_state != ws_b.upload_state


def test_workspace_for_same_principal_is_path_deterministic(projects_root):
    """Calling the factory twice for the same principal+project must
    produce identical paths. Determinism is the property the registry
    binds on, so the workspace factory cannot drift between calls.
    """
    p = Principal(kind="user", principal_id="alice")
    ws_1 = ProjectWorkspace.for_principal(p, "demo")
    ws_2 = ProjectWorkspace.for_principal(p, "demo")
    assert ws_1 == ws_2
    # Equality is structural (frozen dataclass); Path equality is also
    # by string. Belt-and-suspenders:
    assert str(ws_1.root) == str(ws_2.root)
    assert str(ws_1.assets) == str(ws_2.assets)


def test_workspace_for_principal_with_colons_and_dots_still_works(projects_root):
    """principal_id allow-list permits ``.`` and ``-`` (no path separator
    chars). The workspace factory must accept these without complaint —
    the HMAC absorbs whatever survives sanitisation."""
    p = Principal(kind="user", principal_id="alice.bob-charlie")
    ws = ProjectWorkspace.for_principal(p, "demo")
    # ``alice.bob-charlie`` is sanitised; its namespace_key is independent
    # of any ``.``/``-`` content. We just want to see a Path that's a
    # child of the principal root and matches the schema.
    assert ws.root.parent == projects_root / "users" / p.namespace_key


# ---------------------------------------------------------------------------
# 3. resolve() — symlink-aware containment boundary
# ---------------------------------------------------------------------------


def test_resolve_accepts_relative_path_under_root(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    resolved = ws.resolve("assets/foo.png")
    assert resolved == ws.root / "assets" / "foo.png"


def test_resolve_accepts_nested_path(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    resolved = ws.resolve("assets/sub/dir/file.png")
    assert resolved == ws.root / "assets" / "sub" / "dir" / "file.png"


def test_resolve_rejects_path_traversal_dotdot(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("../escape.txt")


def test_resolve_rejects_many_levels_of_traversal(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("assets/../../../etc/passwd")


def test_resolve_rejects_absolute_path(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("/etc/passwd")


def test_resolve_rejects_empty_input(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("")


def test_resolve_rejects_whitespace_only_input(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("   ")


def test_resolve_rejects_none_input(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve(None)  # type: ignore[arg-type]


def test_resolve_rejects_non_string_non_path_input(projects_root):
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve(42)  # type: ignore[arg-type]


def test_resolve_rejects_symlink_escape(projects_root, tmp_path):
    """Plant a symlink inside the workspace that points outside, and
    confirm ``resolve()`` catches the escape.

    ``Path.resolve(strict=False)`` is the symlink-aware step — a plain
    ``relative_to`` check on the un-resolved path would miss this. This
    test exists because the audit explicitly listed
    ``read_session_asset`` as needing the symlink-aware step (v2 doc
    §Phase C line 142).
    """
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    # Actually create the workspace dir so the symlink has a parent.
    ws.root.mkdir(parents=True, exist_ok=True)
    # Plant a symlink at <workspace>/escape -> /etc/passwd (or any
    # outside target the test process can resolve to).
    outside = tmp_path / "outside-secret.bin"
    outside.write_bytes(b"TOP-SECRET")
    symlink_path = ws.root / "escape"
    try:
        symlink_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(WorkspaceErrorError):
        ws.resolve("escape")


def test_workspace_rejects_symlinked_principal_root(projects_root, tmp_path):
    principal = Principal(kind="user", principal_id="alice")
    other = Principal(kind="user", principal_id="bob")
    target = projects_root / "users" / other.namespace_key
    target.mkdir(parents=True)
    link = projects_root / "users" / principal.principal_id
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(WorkspaceErrorError):
        ProjectWorkspace.for_principal(principal, "demo")


def test_resolve_rejects_resolving_outside_root(projects_root, tmp_path):
    """A real file under ``tmp_path`` (outside the workspace tree) must
    be rejected even if the relative input starts with the project
    id. This is the explicit "must not escape" invariant the v2 doc
    pins on ``read_session_asset``."""
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    ws.root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    # The user passes a relative path that, after resolution, lands
    # outside the workspace because ``..`` points there.
    with pytest.raises(WorkspaceErrorError):
        ws.resolve(f"../{outside.name}")


# ---------------------------------------------------------------------------
# 4. sanitize_project_id — allow-list length-cap validation
# ---------------------------------------------------------------------------


def test_sanitize_project_id_accepts_simple_id():
    assert sanitize_project_id("my-project") == "my-project"


def test_sanitize_project_id_accepts_at_length_boundary():
    """64-char project id is the documented max — must be accepted."""
    pid = "a" * MAX_PROJECT_ID_LEN
    assert sanitize_project_id(pid) == pid


def test_sanitize_project_id_rejects_overlong():
    pid = "a" * (MAX_PROJECT_ID_LEN + 1)
    assert sanitize_project_id(pid) is None


@pytest.mark.parametrize(
    "label, bad",
    [
        ("crlf", "abc\r\ndef"),
        ("lf", "abc\ndef"),
        ("dotdot", ".."),
        ("slash", "with/slash"),
        ("backslash", "with\\slash"),
        ("space", "my project"),
        ("unicode_full_width", "ａlice"),
        ("empty", ""),
        ("whitespace_only", "   "),
        ("leading_dash", "-leading"),
        ("leading_dot", ".leading"),
        ("leading_underscore", "_leading"),
        ("non_ascii", "项目A"),
    ],
)
def test_sanitize_project_id_rejects_bad_input(label, bad):
    """Reject conditions mirror ``sanitize_principal_id`` but add a
    tighter ``[A-Za-z0-9]`` first-character rule — ``-``/``.``/``_`` at
    the start would still satisfy ``sanitize_principal_id`` but create
    filesystem oddities (hidden dirs, ambiguous dotfiles)."""
    assert sanitize_project_id(bad) is None, f"should reject {label!r}={bad!r}"


# ---------------------------------------------------------------------------
# 5. for_current_principal() — auth boundary
# ---------------------------------------------------------------------------


def test_for_current_principal_works_when_user_id_ctx_is_set(monkeypatch):
    """When the Phase 3 ContextVar has a user id, the factory builds
    a workspace without touching the registry."""
    import mcp_server
    monkeypatch.setattr(mcp_server, "current_user_id", lambda: "alice_42")
    marker = mcp_server._initialize_request_ctx.set(True)
    try:
        ws = ProjectWorkspace.for_current_principal("demo")
    finally:
        mcp_server._initialize_request_ctx.reset(marker)
    assert ws.principal.kind == "user"
    assert ws.principal.principal_id == "alice_42"
    assert ws.root == lib_paths.PROJECTS_DIR / "users" / ws.principal.namespace_key / "demo"


def test_for_current_principal_raises_when_unbound(monkeypatch):
    """Without a Phase 3 fast-path AND without a Phase B registry
    binding, ``current_principal()`` raises ``PrincipalNotFound`` and
    ``for_current_principal`` lets it propagate untouched. The
    audit / v2 doc explicitly require this: a missing principal is a
    no-write decision, not a silent fallback to a default namespace."""
    import mcp_server
    monkeypatch.setattr(mcp_server, "current_user_id", lambda: None)
    monkeypatch.setattr(mcp_server, "get_mcp_session_id", lambda: None)
    with pytest.raises(PrincipalNotFound):
        ProjectWorkspace.for_current_principal("demo")


def test_for_principal_works_for_service_principal():
    """Service principals flow through the same factory — the v2
    doc explicitly requires both ``kind`` values to be supported."""
    p = Principal(kind="service", principal_id="svc-builder")
    ws = ProjectWorkspace.for_principal(p, "demo")
    assert ws.principal.kind == "service"
    assert "services" in str(ws.root)


# ---------------------------------------------------------------------------
# 6. Asset upload integration — per-user namespace isolation
# ---------------------------------------------------------------------------


def test_asset_upload_uses_per_user_namespace(projects_root, monkeypatch):
    """Two principals simultaneously upload the same filename to the
    same project id; the audit's HIGH #1 risk says they would collide
    under the legacy ``projects/<id>/...`` layout. Phase C: the
    physical on-disk paths must be disjoint."""
    p_alice = Principal(kind="user", principal_id="alice")
    p_bob = Principal(kind="user", principal_id="bob")

    # Same filename, same project_id, two principals. Set up Alice.
    _stub_current_principal(monkeypatch, p_alice)
    sessions = projects_root.parent / "sessions"
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", sessions)
    tool = UploadAsset()
    content = b"\x89PNG\r\n\x1a\nalice-photo"
    digest = hashlib.sha256(content).hexdigest()
    alice_result = tool.execute({
        "project_id": "demo",
        "filename": "photo.png",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": digest,
        "mcp_session_id": "alice-session",
    })
    assert alice_result.success, alice_result.error

    # Now Bob with the same input — different user id, different
    # expected on-disk path.
    _stub_current_principal(monkeypatch, p_bob)
    bob_result = tool.execute({
        "project_id": "demo",
        "filename": "photo.png",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": digest,
        "mcp_session_id": "bob-session",
    })
    assert bob_result.success, bob_result.error

    alice_path = Path(alice_result.artifacts[0])
    bob_path = Path(bob_result.artifacts[0])
    # Sanity: both files exist and read back the same content.
    assert alice_path.exists()
    assert bob_path.exists()
    assert alice_path.read_bytes() == content
    assert bob_path.read_bytes() == content
    # The CRITICAL Phase C assertion: the two paths are disjoint.
    assert alice_path != bob_path, (
        f"per-principal namespace must put alice and bob in disjoint paths; "
        f"got alice={alice_path} bob={bob_path}"
    )
    # And each lives under the correct principal's namespace.
    assert p_alice.namespace_key in str(alice_path)
    assert p_bob.namespace_key in str(bob_path)
    assert "alice" not in str(bob_path)
    assert "bob" not in str(alice_path)


# ---------------------------------------------------------------------------
# 7. Chunked upload integration — state file is per-principal
# ---------------------------------------------------------------------------


def test_chunked_upload_state_is_per_user(projects_root, monkeypatch):
    """The audit's HIGH #2 risk says the legacy
    ``projects/.uploads/<upload_id>.json`` is a shared global
    namespace. Phase C: ``upload_state`` is per-principal, so Alice's
    upload_id cannot be resumed by Bob even if he guesses the id.
    """
    p_alice = Principal(kind="user", principal_id="alice")
    p_bob = Principal(kind="user", principal_id="bob")

    # Alice starts an upload.
    _stub_current_principal(monkeypatch, p_alice)
    monkeypatch.setattr(workbuddy_session, "STATE_DIR", projects_root.parent / "sessions")
    chunk_tool = UploadAssetChunk()
    content = b"\x89PNG\r\n\x1a\nalice-chunked-content"
    digest = hashlib.sha256(content).hexdigest()
    started = chunk_tool.execute({
        "mcp_session_id": "alice-session",
        "operation": "start",
        "project_id": "demo",
        "filename": "photo.png",
        "total_bytes": len(content),
        "mime_type": "image/png",
        "sha256": digest,
    })
    assert started.success, started.error
    upload_id = started.data["upload_id"]

    # Bob, with a *different* session and *different* principal, tries
    # to read the state file by guessing Alice's upload_id. Under the
    # legacy shared-namespace layout he would find it. Under the
    # per-principal layout he finds nothing.
    _stub_current_principal(monkeypatch, p_bob)
    bob_result = chunk_tool.execute({
        "mcp_session_id": "bob-session",
        "operation": "append",
        "upload_id": upload_id,
        "offset": 0,
        "chunk_base64": base64.b64encode(b"x").decode("ascii"),
    })
    assert not bob_result.success
    assert "not found" in bob_result.error.lower(), (
        f"Bob must not be able to resume Alice's upload; got: {bob_result.error}"
    )

    # Sanity: Alice's own state file lives at her principal's .uploads.
    alice_state = (
        projects_root
        / "users"
        / p_alice.namespace_key
        / ".uploads"
        / f"{upload_id}.json"
    )
    assert alice_state.is_file()
    # And it does NOT exist under Bob's namespace (we never started one
    # for him).
    bob_state = (
        projects_root
        / "users"
        / p_bob.namespace_key
        / ".uploads"
        / f"{upload_id}.json"
    )
    assert not bob_state.exists()


# ---------------------------------------------------------------------------
# 8. read_session_asset — namespace containment boundary
# ---------------------------------------------------------------------------


def test_read_session_asset_rejects_other_principal_namespace(
    projects_root, monkeypatch
):
    """The audit's MEDIUM #1 says the legacy ``read_session_asset``
    only checked "in projects/" and let cross-user paths through.
    Phase C layer 3 adds the namespace-key boundary: a relative path
    that resolves to another principal's namespace is rejected even
    when it lives under projects/."""
    from tools.asset import read_session_asset as rsa

    p_alice = Principal(kind="user", principal_id="alice")
    p_bob = Principal(kind="user", principal_id="bob")

    # Plant a file under Bob's namespace.
    bob_project_root = (
        projects_root / "users" / p_bob.namespace_key / "private"
    )
    bob_file = bob_project_root / "assets" / "secret.png"
    bob_file.parent.mkdir(parents=True, exist_ok=True)
    bob_file.write_bytes(b"\x89PNG\r\n\x1a\nSECRET")
    # Express the path repo-root-relative so the tool's Layer 2
    # containment check passes — the Layer 3 namespace check is what
    # should reject it.
    rel_path = str(bob_file.relative_to(projects_root.parent))

    # Alice's principal is bound; her namespace is empty.
    _stub_current_principal(monkeypatch, p_alice)
    tool = rsa.ReadSessionAsset()
    result = tool.execute({"relative_path": rel_path})
    assert not result.success
    error = (result.error or "").lower()
    # We accept either the legacy "outside projects/" wording or the
    # new "outside principal namespace" — both indicate rejection.
    assert "outside" in error or "namespace" in error, (
        f"should reject cross-user path; got: {result.error}"
    )
    # And we never served Bob's bytes back to Alice.
    assert b"SECRET" not in (result.data or {}).get("data_base64", "").encode("ascii", errors="ignore")


# ---------------------------------------------------------------------------
# 9. create_remotion_video_share — workspace anchored to per-principal
# ---------------------------------------------------------------------------


def test_create_remotion_video_share_uses_per_user_root(projects_root, monkeypatch):
    """The audit's HIGH #3 risk says
    ``mcp_server.py:1762 root = projects/project`` was the only entry
    into user-data and had no principal check. After Phase C the root
    comes from ``ProjectWorkspace.for_current_principal(project).root``,
    so two principals sharing the same project_id get disjoint roots
    and the relative-path containment check in the share tool stays
    correct.
    """
    # We don't drive mcp_server end-to-end (no FastMCP server in this
    # test process), but we can exercise the path computation that the
    # tool now uses and assert it matches the per-principal workspace.
    p_alice = Principal(kind="user", principal_id="alice")
    p_bob = Principal(kind="user", principal_id="bob")

    alice_ws = ProjectWorkspace.for_principal(p_alice, "shared")
    bob_ws = ProjectWorkspace.for_principal(p_bob, "shared")

    assert alice_ws.root != bob_ws.root
    assert alice_ws.root == projects_root / "users" / p_alice.namespace_key / "shared"
    assert bob_ws.root == projects_root / "users" / p_bob.namespace_key / "shared"

    # The share tool validates session-asset paths against
    # ``path.relative_to(root.resolve())``. Bob's file in Bob's
    # namespace must fail this check when ``root`` is Alice's — that's
    # exactly the boundary Phase C introduces.
    bob_file = bob_ws.root / "assets" / "secret.png"
    bob_file.parent.mkdir(parents=True, exist_ok=True)
    bob_file.write_bytes(b"x")
    with pytest.raises(ValueError):
        bob_file.relative_to(alice_ws.root.resolve())
    # Mirror in the other direction.
    alice_file = alice_ws.root / "assets" / "alice.png"
    alice_file.parent.mkdir(parents=True, exist_ok=True)
    alice_file.write_bytes(b"y")
    # Alice's own file passes under Alice's root.
    assert alice_file.relative_to(alice_ws.root.resolve()) == Path("assets/alice.png")
    with pytest.raises(ValueError):
        alice_file.relative_to(bob_ws.root.resolve())


# ---------------------------------------------------------------------------
# 10. Pathological defensive checks
# ---------------------------------------------------------------------------


def test_for_principal_rejects_bad_project_id():
    """A bad project id is a programmer/contract error — the factory
    raises ``ValueError``. The audit's design goal is "a malicious
    caller cannot smuggle a path component" — sanitisation must run
    inside the factory so callers cannot forget it."""
    p = Principal(kind="user", principal_id="alice")
    with pytest.raises(ValueError):
        ProjectWorkspace.for_principal(p, "../escape")


def test_frozen_dataclass_rejects_mutation():
    """Frozen dataclass — mutation raises. Belt-and-suspenders against
    a caller trying to override ``root`` post-construction to point
    outside the principal namespace."""
    p = Principal(kind="user", principal_id="alice")
    ws = ProjectWorkspace.for_principal(p, "demo")
    with pytest.raises((AttributeError, Exception)):
        ws.root = Path("/etc/passwd")  # type: ignore[misc]


# Imports used by fixture helpers below — keep them at the bottom so the
# ``Path`` symbol is only needed once the helpers above reference it.
from pathlib import Path  # noqa: E402  (deliberate: avoid leaking Path into module docstring)
