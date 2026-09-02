"""Read a session-uploaded asset by repo-relative path and return its bytes.

This tool exists so the BFF (which lives on a different host than the MCP
server that received the upload) can serve thumbnails without needing a
shared filesystem. The MCP server is the authoritative storage location;
the BFF used to ``os.Stat`` and ``c.File`` directly off its own
``projects/`` tree, which 404'd for every upload because the file only
exists on the MCP host.

Inputs:
    - ``relative_path`` (required): OS-portable path, repo-root-relative.
      Must live under ``<repo>/projects/``; ``..`` and absolute prefixes
      are rejected.
    - ``mcp_session_id`` (optional): forwarded by ``_run_tool_sync`` for
      observability / future owner-scope checks. The whitelist check is
      performed by the BFF; this tool focuses on path safety and reading.

Returns:
    ``{"bytes": int, "data_base64": str, "mime_type": str, "filename": str,
      "relative_path": str}`` on success. ``success=False, error=...`` on
    validation failure or missing file.
"""
from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from lib import paths as lib_paths
from lib.principal_registry import PrincipalNotFound
from lib.project_workspace import ProjectWorkspace, WorkspaceErrorError
from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier


# Same resolution rule as the upload tools: repo root is two parents up
# from this file (``tools/asset/read_session_asset.py`` -> ``tools/asset`` ->
# ``tools`` -> repo root). Phase C still needs this for the
# absolute-path rejection step, but the principal namespace boundary is
# checked via ``ProjectWorkspace``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROJECTS_ROOT = (_REPO_ROOT / "projects").resolve()


class ReadSessionAsset(BaseTool):
    name = "read_session_asset"
    version = "1.0.0"
    tier = ToolTier.CORE
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "asset_management"
    provider = "openmontage"
    capabilities = ["asset_read", "thumbnail", "session_asset"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, disk_mb=0)
    side_effects = ["reads bytes from projects/users/<ns>/<id>/assets/_sessions/*"]
    input_schema = {
        "type": "object",
        "required": ["relative_path"],
        "properties": {
            "relative_path": {"type": "string", "minLength": 1},
        },
    }

    @staticmethod
    def _validate_relative(rel: str) -> Path:
        """Validate ``relative_path`` against the current principal.

        Phase C adds the namespace-key boundary on top of the legacy
        ``projects/`` check. A path that lives under
        ``projects/<user_or_service>/<other_namespace_key>/...`` is now
        rejected even if the legacy prefix check would let it through —
        see v2 doc §Phase C line 142: ``read_session_asset`` must
        independently verify the requested resolved path stays inside the
        current principal's namespace.

        Three layers, fail-closed at every step:

        1. Format: non-empty string, no ``..`` after normalization, no
           absolute prefix. This is the pre-Phase-C check; it stops
           obvious path-traversal attempts.
        2. Repo containment: the resolved path must live under
           ``<repo>/projects/``. Catches relative escapes that the
           format check missed (e.g. ``projects/../escape``).
        3. Principal containment (Phase C): the resolved path must live
           under the current principal's
           ``projects/users/<namespace_key>/`` (or ``services/``)
           subtree. Catches any cross-user attempts that pass steps 1+2.
        """
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError("relative_path is required")
        # Layer 1: format. ``os.path.normpath`` collapses ``..`` so we
        # check the normalized form, not the raw input.
        norm = os.path.normpath(rel.replace("\\", "/"))
        if norm.startswith("..") or os.path.isabs(norm) or norm.startswith("/"):
            raise ValueError(f"relative_path escapes repo root: {rel!r}")
        # Layer 2: repo / projects containment. Same prefix-with-sep
        # comparison as before so a directory like
        # ``/opt/OpenMontage/projects-evil`` cannot pass the prefix
        # check.
        # Uploads encode paths relative to ``PROJECTS_DIR.parent``. Resolve
        # against that same configured anchor instead of the source checkout
        # so a custom OPENMONTAGE_PROJECTS_DIR round-trips correctly.
        configured_projects_root = Path(lib_paths.PROJECTS_DIR).resolve()
        configured_repo_root = configured_projects_root.parent
        candidate = (configured_repo_root / norm).resolve()
        projects_with_sep = str(configured_projects_root) + os.sep
        if not (str(candidate) + os.sep).startswith(projects_with_sep) and str(candidate) != str(configured_projects_root):
            raise ValueError(f"relative_path outside projects/: {rel!r}")
        # Layer 3: per-principal namespace boundary. The audit's MEDIUM #1
        # says the legacy check let cross-user paths through; this
        # layer enforces the v2 doc §Phase C line 142 invariant that
        # ``read_session_asset`` must independently verify the
        # resolved path stays inside the current principal's namespace.
        #
        # We do NOT call ``principal_workspace.resolve(norm)`` —
        # ``resolve`` treats the input as *relative to the workspace
        # root*, which would interpret ``projects/users/<other_ns>/...``
        # as a subdirectory of the workspace and pass. The right check
        # is: the resolved absolute path ``candidate`` (computed in
        # Layer 2) must be under the principal's namespace root.
        try:
            principal_workspace = ProjectWorkspace.for_current_principal("lookup")
        except (WorkspaceErrorError, PrincipalNotFound):
            # If no binding or the workspace factory itself rejects,
            # fall through to the same opaque error. Production callers
            # should still see a 403 from a wrapper that catches
            # ``PrincipalNotFound`` earlier; this layer's job is to
            # refuse the read, not to surface auth status.
            raise ValueError(f"relative_path outside principal namespace: {rel!r}")
        # Compare ``candidate`` against the principal's namespace root
        # (the workspace's root without its terminal project_id). The
        # ``principal_workspace.root`` has the dummy project_id
        # ``"lookup"`` baked in; ``.parent`` strips it so the boundary
        # is the per-principal namespace, not a single project inside
        # it. We resolve the principal root via ``Path.resolve(strict=False)``
        # so a planted symlink that lands outside still escapes.
        # During the migration window the authenticated principal has two
        # allowed roots: canonical v2 and its raw-id v1 fallback.  Keep the
        # check principal-scoped; never infer ownership from a 32-hex path
        # segment or accept any other user's namespace.
        allowed_namespace_roots = tuple(
            candidate_root.parent.resolve(strict=False)
            for candidate_root in principal_workspace.read_roots
        )
        if not any(
            candidate == namespace_root
            or namespace_root in candidate.parents
            for namespace_root in allowed_namespace_roots
        ):
            raise ValueError(f"relative_path outside principal namespace: {rel!r}")
        return candidate

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = _now()
        try:
            rel = inputs.get("relative_path")
            abs_path = self._validate_relative(rel)
            if not abs_path.exists() or not abs_path.is_file():
                return ToolResult(
                    success=False,
                    error=f"file not found at {rel}",
                    duration_seconds=_now() - started,
                )
            data = abs_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(abs_path))
            encoded = base64.b64encode(data).decode("ascii")
            return ToolResult(
                success=True,
                data={
                    "bytes": len(data),
                    "data_base64": encoded,
                    "mime_type": mime or "application/octet-stream",
                    "filename": abs_path.name,
                    "relative_path": rel,
                },
                artifacts=[str(abs_path)],
                duration_seconds=_now() - started,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                duration_seconds=_now() - started,
            )


def _now() -> float:
    import time

    return time.monotonic()
