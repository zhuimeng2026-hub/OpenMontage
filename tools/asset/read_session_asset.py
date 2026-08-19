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

from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier


# Same resolution rule as the upload tools: repo root is two parents up
# from this file (``tools/asset/read_session_asset.py`` -> ``tools/asset`` ->
# ``tools`` -> repo root).
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
    side_effects = ["reads bytes from projects/<id>/assets/_sessions/*"]
    input_schema = {
        "type": "object",
        "required": ["relative_path"],
        "properties": {
            "relative_path": {"type": "string", "minLength": 1},
        },
    }

    @staticmethod
    def _validate_relative(rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ValueError("relative_path is required")
        # Normalize and reject absolute prefixes + traversal. ``os.path.normpath``
        # collapses ``..`` so we check the *normalized* form, not the raw input.
        norm = os.path.normpath(rel.replace("\\", "/"))
        if norm.startswith("..") or os.path.isabs(norm) or norm.startswith("/"):
            raise ValueError(f"relative_path escapes repo root: {rel!r}")
        candidate = (_REPO_ROOT / norm).resolve()
        # Defense in depth: ensure the resolved path is still under projects/.
        # Compare against the pre-resolved projects root with a trailing sep so
        # /opt/OpenMontage/projects-evil cannot pass the prefix check.
        projects_with_sep = str(_PROJECTS_ROOT) + os.sep
        if not (str(candidate) + os.sep).startswith(projects_with_sep) and str(
            candidate
        ) != str(_PROJECTS_ROOT):
            raise ValueError(f"relative_path outside projects/: {rel!r}")
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