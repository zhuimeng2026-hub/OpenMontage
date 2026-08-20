"""Secure project-scoped asset upload for the OpenMontage MCP server.

The MCP client cannot pass a client-local Windows path to a remote renderer.
This tool accepts base64 content, stores it below the OpenMontage project
workspace, and returns an ``asset_manifest``-compatible record.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier
from lib.workbuddy_session import register_asset, register_image, require_session, session_hash


_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
    ".mp4", ".webm", ".mov", ".m4v", ".avi",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg",
}


def _max_upload_bytes() -> int:
    raw = os.environ.get("OPENMONTAGE_MAX_UPLOAD_MB", "100")
    try:
        return max(1, int(raw)) * 1024 * 1024
    except ValueError:
        return 100 * 1024 * 1024


def _session_key(session_id: Any) -> str:
    """Return a filesystem-safe namespace for one MCP session."""
    return require_session(session_id)


class UploadAsset(BaseTool):
    """Upload one image/video/audio asset into a project workspace."""

    name = "upload_asset"
    version = "1.0.0"
    tier = ToolTier.CORE
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "asset_management"
    provider = "openmontage"
    capabilities = ["asset_upload", "project_asset_storage"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=100, network_required=False)
    side_effects = ["writes uploaded bytes below projects/<project_id>/assets"]
    best_for = ["uploading client-local media before remote Remotion or AI video rendering"]
    not_good_for = ["very large files; use a presigned object-storage flow instead"]
    input_schema = {
        "type": "object",
        "required": ["project_id", "filename", "content_base64"],
        "properties": {
            "project_id": {
                "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                "description": "OpenMontage project id; path separators are forbidden.",
            },
            "filename": {
                "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$",
                "description": "Original filename. Only a safe basename and supported media extension are accepted.",
            },
            "content_base64": {
                "type": "string",
                "description": "Raw base64 or a data:...;base64,... URI.",
            },
            "mime_type": {"type": "string", "description": "Optional MIME type for metadata."},
            "sha256": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$", "description": "Optional integrity hash."},
            "overwrite": {"type": "boolean", "default": False, "description": "Replace an existing file with the same name."},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "asset": {"type": "object"},
            "asset_manifest": {"type": "object"},
            "deduplicated": {"type": "boolean"},
        },
    }

    @staticmethod
    def _workspace_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def _project_dir(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id):
            raise ValueError("project_id must be a safe basename (letters, numbers, '.', '_' and '-' only)")
        root = (self._workspace_root() / "projects").resolve()
        project_dir = (root / project_id).resolve()
        try:
            project_dir.relative_to(root)
        except ValueError as exc:
            raise ValueError("project_id escapes the projects directory") from exc
        return project_dir

    @staticmethod
    def _decode_content(value: Any) -> bytes:
        if not isinstance(value, str) or not value:
            raise ValueError("content_base64 is required")
        if value.startswith("data:"):
            marker = ";base64,"
            if marker not in value:
                raise ValueError("data URI must contain ;base64,")
            value = value.split(marker, 1)[1]
        value = re.sub(r"\s+", "", value)
        try:
            content = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        if not content:
            raise ValueError("uploaded content is empty")
        if len(content) > _max_upload_bytes():
            raise ValueError(f"uploaded content exceeds {_max_upload_bytes() // (1024 * 1024)} MB limit")
        return content

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = __import__("time").monotonic()
        try:
            project_id = inputs.get("project_id")
            filename = inputs.get("filename")
            if not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename):
                raise ValueError("filename must be a safe basename without path separators")
            suffix = Path(filename).suffix.lower()
            if suffix not in _ALLOWED_EXTENSIONS:
                raise ValueError(f"unsupported media extension: {suffix or '<none>'}")

            content = self._decode_content(inputs.get("content_base64"))
            digest = hashlib.sha256(content).hexdigest()
            expected = inputs.get("sha256")
            if expected and str(expected).lower() != digest:
                raise ValueError("sha256 does not match uploaded content")

            project_dir = self._project_dir(project_id)
            session_id = inputs.get("mcp_session_id")
            session_digest = require_session(session_id)
            assets_dir = project_dir / "assets" / "_sessions" / session_digest
            assets_dir.mkdir(parents=True, exist_ok=True)
            target = (assets_dir / filename).resolve()
            try:
                target.relative_to(assets_dir.resolve())
            except ValueError as exc:
                raise ValueError("filename escapes the project assets directory") from exc

            if target.exists() and not inputs.get("overwrite", False):
                existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if existing_digest == digest:
                    deduplicated = True
                else:
                    raise ValueError("asset already exists; set overwrite=true to replace it")
            else:
                fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".upload", dir=str(assets_dir))
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, target)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                deduplicated = False

            relative_path = target.relative_to(self._workspace_root()).as_posix()
            mime_type = inputs.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            asset = {
                "id": f"{project_id}-{digest[:12]}",
                "filename": filename,
                "path": str(target),
                "relative_path": relative_path,
                "type": "image" if mime_type.startswith("image/") else "video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else "media",
                "mime_type": mime_type,
                "bytes": len(content),
                "sha256": digest,
                "source": "mcp_upload",
                "session_hash": session_hash(session_id),
            }
            batch = None
            if asset["type"] == "image":
                batch = register_image(session_id, project_id, asset)
            else:
                register_asset(session_id, project_id, asset)
            return ToolResult(
                success=True,
                data={
                    "asset": asset,
                    "asset_manifest": {"assets": [asset]},
                    "deduplicated": deduplicated,
                    **({"batch": batch} if batch else {}),
                },
                artifacts=[str(target)],
                duration_seconds=__import__("time").monotonic() - started,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=__import__("time").monotonic() - started)
