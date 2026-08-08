"""Upload images to imgbb (imgbb.com) for public URL retrieval."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
    ToolStatus,
)


class ImgbbUpload(BaseTool):
    name = "imgbb_upload"
    version = "0.1.0"
    tier = ToolTier.SOURCE
    capability = "image_upload"
    provider = "imgbb"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "imgbb upload uses the public API key already set in .env as imgbb_key.\n"
        "No additional configuration required."
    )

    capabilities = ["upload_image", "get_public_url"]
    supports = {
        "custom_expiration": True,
        "delete_url": True,
        "base64_input": True,
        "file_path_input": True,
    }
    best_for = [
        "uploading product images for Kling image-to-video",
        "getting public URLs from local image files",
        "temporarily hosting images for AI video generation",
    ]
    not_good_for = [
        "hosting sensitive/private images",
        "large video files (imgbb max 32MB per file)",
        "permanent archival (use cloud storage instead)",
    ]
    fallback_tools = []

    input_schema = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Local file path to the image to upload",
            },
            "image_base64": {
                "type": "string",
                "description": "Base64-encoded image data (alternative to image_path)",
            },
            "expiration": {
                "type": "integer",
                "default": 0,
                "minimum": 0,
                "maximum": 15552000,
                "description": "Auto-delete after N seconds. 0 = permanent (default)",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=0.1, ram_mb=128, vram_mb=0, disk_mb=10, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = []
    side_effects = ["uploads file to imgbb.com", "returns public URL and delete URL"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def _get_api_key(self) -> str:
        return os.environ.get("imgbb_key", "")

    def _upload(self, image_data: bytes, expiration: int = 0) -> dict[str, Any]:
        """Upload image to imgbb and return parsed response."""
        api_key = self._get_api_key()
        if not api_key:
            raise RuntimeError("imgbb_key is not set in environment")

        url = "https://api.imgbb.com/1/upload"
        params = {"key": api_key}
        data = {"expiration": str(expiration)}

        r = requests.post(
            url,
            params=params,
            data=data,
            files={"image": ("upload.jpg", image_data, "image/jpeg")},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()

        if not resp.get("success"):
            raise RuntimeError(f"imgbb upload failed: {resp.get('error', {}).get('message', resp)}")

        return resp.get("data", {})

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        expiration = int(inputs.get("expiration", 0))

        # Get image data from path or base64
        image_data: bytes | None = None
        if inputs.get("image_path"):
            p = Path(inputs["image_path"])
            if not p.is_file():
                return ToolResult(success=False, error=f"File not found: {p}")
            image_data = p.read_bytes()
        elif inputs.get("image_base64"):
            import base64
            try:
                image_data = base64.b64decode(inputs["image_base64"])
            except Exception as e:
                return ToolResult(success=False, error=f"Invalid base64: {e}")
        else:
            return ToolResult(
                success=False,
                error="Provide either image_path or image_base64",
            )

        try:
            data = self._upload(image_data, expiration)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=True,
            data={
                "url": data.get("image", {}).get("url", ""),
                "delete_url": data.get("delete_url", ""),
                "expiration": data.get("expiration", expiration),
                "filename": data.get("image", {}).get("filename", ""),
                "size": data.get("image", {}).get("size", 0),
            },
            duration_seconds=round(time.time() - start, 2),
        )
