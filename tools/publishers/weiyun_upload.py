"""Weiyun (腾讯微云) MCP-token upload tool — PUBLISH tier.

Uploads a finished render to Tencent Weiyun via the official MCP endpoint using
the MCP token (``WEIYUN_MCP_TOKEN``). This is the token-based path that does
NOT require QR-code login or cookies — it reuses the proven FTN two-phase upload
logic vendored into ``weiyun_upload_lib.py`` (from the WorkBuddy weiyun skill).

This is the supported Weiyun upload path for OpenMontage. It only needs the
MCP token, which is the same credential the Tencent Weiyun MCP skill uses.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)
from .weiyun_upload_lib import upload_file


class WeiyunUpload(BaseTool):
    name = "weiyun_upload"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "weiyun"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    # The tool is reported unavailable until WEIYUN_MCP_TOKEN is set.
    dependencies = ["env:WEIYUN_MCP_TOKEN"]
    install_instructions = (
        "Set WEIYUN_MCP_TOKEN in the OpenMontage `.env` (the same token the "
        "Tencent Weiyun MCP skill uses). No QR-code login or cookies required. "
        "Also ensure `requests` is installed (pip install requests)."
    )

    capabilities = ["upload_to_weiyun"]
    supports = {
        "local_offline": False,
        "free": False,  # consumes the operator's Weiyun storage/quota
        "uploads": True,
    }
    best_for = [
        "delivering a finished render to the operator's Weiyun netdisk",
        "hosting the original video file on Weiyun (token-based, no cookie login)",
    ]
    not_good_for = [
        "direct posting to YouTube/TikTok/Bilibili (Weiyun is a netdisk, not a social host)",
        "transcoding or thumbnail generation (handled by other pipeline stages)",
    ]

    input_schema = {
        "type": "object",
        "required": ["video_path"],
        "properties": {
            "video_path": {
                "type": "string",
                "description": "Path to the final rendered video to upload (e.g. render output path).",
            },
            "target_dir": {
                "type": "string",
                "description": "Optional Weiyun directory key (pdir_key). Empty = token's default directory.",
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "Kept for API parity; the FTN protocol overwrites by file identity regardless.",
            },
            "mcp_session_id": {
                "type": "string",
                "description": "Server-injected MCP session identifier used to correlate the result.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "Weiyun file id of the uploaded file."},
            "filename": {"type": "string", "description": "Filename on Weiyun."},
            "mcp_url": {"type": "string", "description": "MCP endpoint used."},
            "target_dir": {"type": "string", "description": "Directory key used (empty = default)."},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=0, network_required=True
    )
    side_effects = [
        "uploads a video file to the operator's Tencent Weiyun account",
    ]
    user_visible_verification = [
        "Open Weiyun and confirm the file appears in the target directory",
        "Optionally generate a share link from the returned file_id to deliver to the customer",
    ]

    # ---- config helpers ----

    def _mcp_url(self) -> str:
        return os.environ.get("WEIYUN_MCP_URL", "https://www.weiyun.com/api/v3/mcpserver")

    def _build_headers(self) -> dict[str, str]:
        token = (os.environ.get("WEIYUN_MCP_TOKEN") or "").strip()
        if not token:
            raise DependencyError(
                "WEIYUN_MCP_TOKEN is not set. " + self.install_instructions
            )
        headers = {
            "Content-Type": "application/json",
            "WyHeader": f"mcp_token={token}",
        }
        env_id = os.environ.get("WEIYUN_ENV_ID")
        if env_id:
            headers["Cookie"] = f"env_id={env_id}"
        return headers

    # ---- dependency gating ----

    def check_dependencies(self) -> None:
        """Require the MCP token AND the requests package."""
        super().check_dependencies()
        try:
            import requests  # noqa: F401  (validates the package is installed)
        except ImportError:
            raise DependencyError(
                "Python package 'requests' is not installed. pip install requests"
            )

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        video_path = Path(inputs["video_path"]).expanduser()
        if not video_path.is_file():
            return ToolResult(success=False, error=f"video_path not found: {video_path}")

        try:
            headers = self._build_headers()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        mcp_url = self._mcp_url()
        pdir_key = (inputs.get("target_dir") or "").strip() or None

        started = time.time()
        try:
            result = upload_file(
                file_path=str(video_path),
                mcp_url=mcp_url,
                headers=headers,
                pdir_key=pdir_key,
            )
        except Exception as exc:  # noqa: BLE001 - surface any upload failure to the caller
            return ToolResult(success=False, error=f"Weiyun upload failed: {exc}")
        elapsed = round(time.time() - started, 2)

        return ToolResult(
            success=True,
            data={
                "file_id": result.get("file_id"),
                "filename": result.get("filename"),
                "mcp_url": mcp_url,
                "target_dir": pdir_key or "",
                "mcp_session_id": inputs.get("mcp_session_id"),
            },
            artifacts=[str(video_path)],
            duration_seconds=elapsed,
        )
