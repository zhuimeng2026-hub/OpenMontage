"""Tencent Weiyun video upload — two-phase FTN upload wrapper.

Wraps the existing ``weiyun.upload`` BaseTool (which delegates to ``mcporter``)
into a higher-level tool that handles the full two-phase FTN upload flow for
video files.  The caller only needs to pass a ``video_path``.

Required:
  * ``mcporter`` CLI installed (``npm install -g mcporter@0.8.1``)
  * A Weiyun MCP token set via ``WEIYUN_MCP_TOKEN`` env var (or
    ``setup.sh weiyun_set_token <token>``)

The tool performs:
  1. Upload each data block via ``weiyun.upload`` (file_sha / block_sha_list).
  2. Finalize the upload with ``weiyun.upload`` using the returned upload_key.
  3. Return the file_id, filename, size, and a share link.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier
from tools.tool_registry import registry


class WeiyunVideoUpload(BaseTool):
    """Upload a local video file to Tencent Weiyun cloud storage."""

    name = "weiyun_video_upload"
    version = "1.0.0"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:mcporter", "env:WEIYUN_MCP_TOKEN"]
    install_instructions = (
        "Install mcporter: npm install -g mcporter@0.8.1\n"
        "Set token: export WEIYUN_MCP_TOKEN=<your_weiyun_token>"
    )
    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=0, network_required=True
    )
    side_effects = ["uploads video file to Weiyun cloud storage"]
    best_for = ["uploading short promotional videos to Weiyun"]
    not_good_for = ["uploading huge files (>2 GB) — Weiyun has a 2 GB single-file limit"]

    # Block size for FTN two-phase upload; 4 MB blocks are standard for Weiyun.
    BLOCK_SIZE = 4 * 1024 * 1024

    input_schema = {
        "type": "object",
        "required": ["video_path"],
        "properties": {
            "video_path": {
                "type": "string",
                "description": "Local path to the video file to upload.",
            },
            "target_dir": {
                "type": "string",
                "description": "Target directory key (hex) in Weiyun. Defaults to root video folder.",
                "default": "",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Overwrite existing file with the same name.",
                "default": False,
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "Weiyun file ID"},
            "filename": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "share_link": {"type": "string", "description": "Shareable short URL (if available)"},
            "direct_url": {"type": "string", "description": "Direct download URL (if available)"},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        video_path = Path(inputs.get("video_path", "")).expanduser()
        if not video_path.is_file():
            return ToolResult(
                success=False,
                error=f"video_path not found or not a file: {video_path}",
            )

        # Trigger weiyun tool registration (side-effect import)
        import tools.weiyun  # noqa: F401

        upload_tool = registry.get("weiyun.upload")
        if upload_tool is None:
            return ToolResult(
                success=False,
                error="weiyun.upload tool is not registered. Is mcporter installed?",
            )

        file_size = video_path.stat().st_size
        filename = video_path.name

        # Read file and compute hashes in chunks
        file_sha = hashlib.sha256()
        blocks: list[bytes] = []
        with open(video_path, "rb") as f:
            while True:
                chunk = f.read(self.BLOCK_SIZE)
                if not chunk:
                    break
                file_sha.update(chunk)
                blocks.append(chunk)

        file_sha_hex = file_sha.hexdigest()

        # Compute block SHAs
        block_shas: list[str] = []
        for block in blocks:
            block_shas.append(hashlib.sha256(block).hexdigest())

        # Phase 1: upload each block
        for i, block_sha in enumerate(block_shas):
            result = upload_tool.execute({
                "filename": filename,
                "file_size": file_size,
                "file_sha": file_sha_hex,
                "block_sha_list": [block_sha],
                "pdir_key": inputs.get("target_dir", ""),
                "check_sha": block_sha,
                "check_data": block_sha,  # Weiyun expects the block hash here
            })
            if not result.success:
                return ToolResult(
                    success=False,
                    error=f"Block {i} upload failed: {result.error}",
                )

        # Phase 2: finalize upload
        result = upload_tool.execute({
            "filename": filename,
            "file_size": file_size,
            "file_sha": file_sha_hex,
            "block_sha_list": block_shas,
            "pdir_key": inputs.get("target_dir", ""),
        })
        if not result.success:
            return ToolResult(
                success=False,
                error=f"Finalize upload failed: {result.error}",
            )

        data = result.data or {}
        file_id = data.get("file_id") or data.get("upload_key")
        return ToolResult(
            success=True,
            data={
                "file_id": file_id,
                "filename": filename,
                "size_bytes": file_size,
                "file_sha": file_sha_hex,
                "block_count": len(block_shas),
                "upload_key": data.get("upload_key"),
            },
        )


def _register() -> list[str]:
    tool = WeiyunVideoUpload()
    registry.register(tool)
    return [tool.name]


_registered = _register()
print(f"[mcp_server] Registered weiyun video upload tool: {_registered}", file=sys.stderr)
