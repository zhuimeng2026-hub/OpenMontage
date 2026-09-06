"""Read a session-uploaded image asset and return it as an MCP image block.

Sibling of :mod:`tools.asset.read_session_asset`, which returns a plain dict
carrying ``data_base64``. That dict is a contract the BFF depends on to render
thumbnails on a different host, so its shape cannot change. This tool exists
for the other consumer: MCP clients that render ``ImageContent`` natively.

``mcp.server.fastmcp.utilities.func_metadata._convert_to_content`` only emits
``ImageContent(type="image", ...)`` when a tool returns an ``Image`` object —
dicts, pydantic models and strings all degrade to ``TextContent``. Returning
``Image`` is therefore the only thing that makes a client actually draw the
picture instead of dumping base64 text.

Path safety is delegated to ``ReadSessionAsset._validate_relative`` so both
tools enforce exactly one containment rule (repo-root-relative, must resolve
under ``<repo>/projects/``).

Inputs:
    - ``relative_path`` (required): OS-portable path, repo-root-relative.

Returns:
    ``ToolResult(success=True, data={"image": Image, ...})``. The MCP entry
    point must return ``result.data["image"]`` — the ``Image`` object itself —
    for FastMCP to emit an image block; handing back the whole dict would
    degrade to TextContent (and is not JSON-serializable anyway).

    ``ToolResult(success=False, error=...)`` for paths that escape the repo,
    missing files, non-image extensions, and files too large to survive
    base64 inflation.
"""
from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp.utilities.types import Image

from tools.asset.read_session_asset import ReadSessionAsset
from tools.asset_upload import _max_upload_bytes
from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier


# Mirrors the extension -> format table inside mcp's ``Image._get_mime_type``.
# Anything outside this set would come back as ``application/octet-stream``,
# which no client can render, so we reject it up front with an actionable
# error rather than shipping a broken image block.
_SUPPORTED_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}


def _max_image_bytes() -> int:
    """Raw-byte budget whose base64 form still fits the shared upload envelope.

    base64 inflates a payload by ~33%, and the MCP response travels back
    through the same reverse proxy as uploads, so the limit is sized against
    the *encoded* size rather than the on-disk size.
    """
    return _max_upload_bytes() * 3 // 4


class ReadSessionAssetImage(BaseTool):
    name = "read_session_asset_image"
    version = "1.0.0"
    tier = ToolTier.CORE
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "asset_management"
    provider = "openmontage"
    capabilities = ["asset_read", "image_content", "session_asset"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, disk_mb=0)
    side_effects = ["reads bytes from projects/<id>/assets/_sessions/*"]
    input_schema = {
        "type": "object",
        "required": ["relative_path"],
        "properties": {
            "relative_path": {"type": "string", "minLength": 1},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            rel = inputs.get("relative_path")
            # Single source of truth for path safety, shared with read_session_asset.
            abs_path = ReadSessionAsset._validate_relative(rel)

            if not abs_path.is_file():
                return ToolResult(
                    success=False,
                    error=f"file not found at {rel}",
                    duration_seconds=time.monotonic() - started,
                )

            suffix = abs_path.suffix.lower()
            fmt = _SUPPORTED_FORMATS.get(suffix)
            if fmt is None:
                return ToolResult(
                    success=False,
                    error=(
                        f"unsupported image format {suffix or '<none>'}: "
                        f"read_session_asset_image supports "
                        f"{', '.join(sorted(_SUPPORTED_FORMATS))}; "
                        "use read_session_asset for non-image assets "
                        "(mp4/srt/mp3/...)"
                    ),
                    duration_seconds=time.monotonic() - started,
                )

            limit = _max_image_bytes()
            size = abs_path.stat().st_size
            if size > limit:
                return ToolResult(
                    success=False,
                    error=(
                        f"image too large for an MCP image block: {size} bytes "
                        f"on disk exceeds the {limit}-byte limit "
                        f"(base64 would add ~33%)"
                    ),
                    duration_seconds=time.monotonic() - started,
                )

            data = abs_path.read_bytes()
            if not data:
                return ToolResult(
                    success=False,
                    error=f"file is empty at {rel}",
                    duration_seconds=time.monotonic() - started,
                )

            return ToolResult(
                success=True,
                data={
                    "image": Image(data=data, format=fmt),
                    "bytes": len(data),
                    # Same metadata keys as read_session_asset (minus the
                    # base64 payload) so both tools log alike.
                    "mime_type": f"image/{fmt}",
                    "filename": abs_path.name,
                    "relative_path": rel,
                },
                artifacts=[str(abs_path)],
                duration_seconds=time.monotonic() - started,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                duration_seconds=time.monotonic() - started,
            )
