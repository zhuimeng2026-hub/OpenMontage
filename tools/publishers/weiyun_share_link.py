"""Weiyun (腾讯微云) MCP-token share-link tool — PUBLISH tier.

Generates a shareable short URL for files/dirs already stored in Tencent Weiyun
via the official MCP endpoint, using the MCP token (``WEIYUN_MCP_TOKEN``). This
is the token-based counterpart to the cookie/mcporter-based
``weiyun.gen_share_link`` tool and requires NO ``mcporter`` CLI install.

It reuses the proven HTTP transport vendored into ``weiyun_upload_lib.mcp_call``
(the same endpoint, auth header, and retry logic that the upload tool uses), so
the only credential needed is the MCP token already configured in ``.env``.
"""

from __future__ import annotations

import os
import time
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
from .weiyun_upload_lib import mcp_call


class WeiyunShareLink(BaseTool):
    name = "weiyun_share_link"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "weiyun"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    # Token-based: needs WEIYUN_MCP_TOKEN, not QR-code login / cookies / mcporter.
    dependencies = ["env:WEIYUN_MCP_TOKEN"]
    install_instructions = (
        "Set WEIYUN_MCP_TOKEN in the OpenMontage `.env` (the same token the "
        "Tencent Weiyun MCP skill uses). No QR-code login, cookies, or the "
        "mcporter CLI are required. Also ensure `requests` is installed."
    )

    capabilities = ["gen_weiyun_share_link"]
    supports = {
        "local_offline": False,
        "free": False,
        "uploads": False,
    }
    best_for = [
        "creating a shareable short URL for a Weiyun file/dir",
        "delivering a finished render to a customer via a share link",
    ]
    not_good_for = [
        "uploading files (use the weiyun_upload tool for that)",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "file_list": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of Weiyun file ids to share.",
            },
            "dir_list": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of Weiyun directory keys to share.",
            },
            "share_name": {
                "type": "string",
                "description": "Optional display name for the share.",
            },
            "passwd": {
                "type": "string",
                "description": "Optional 6-char share password.",
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
            "short_url": {"type": "string", "description": "Shareable short URL."},
            "share_name": {"type": "string", "description": "Share display name."},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True
    )
    side_effects = ["creates a share link in Weiyun"]
    user_visible_verification = [
        "Open the returned short_url to confirm the share is accessible",
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
        file_list = inputs.get("file_list") or []
        dir_list = inputs.get("dir_list") or []
        share_name = (inputs.get("share_name") or "").strip()
        passwd = (inputs.get("passwd") or "").strip()

        if not file_list and not dir_list:
            return ToolResult(
                success=False,
                error="file_list or dir_list is required to generate a share link",
            )

        try:
            headers = self._build_headers()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        mcp_url = self._mcp_url()
        args: dict[str, Any] = {}
        # The Weiyun MCP gen_share_link tool expects file_list / dir_list as a
        # list of *objects* ({"file_id": ...} / {"dir_key": ...}), not strings.
        if file_list:
            args["file_list"] = [{"file_id": f} for f in file_list]
        if dir_list:
            args["dir_list"] = [{"dir_key": d} for d in dir_list]
        if share_name:
            args["share_name"] = share_name
        if passwd:
            args["passwd"] = passwd

        started = time.time()
        try:
            res = mcp_call(mcp_url, headers, "weiyun.gen_share_link", args)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
            return ToolResult(success=False, error=f"Weiyun gen_share_link failed: {exc}")
        elapsed = round(time.time() - started, 2)

        if res.get("error"):
            return ToolResult(success=False, error=f"weiyun error: {res.get('error')}")

        short_url = (
            res.get("short_url")
            or res.get("share_url")
            or (res.get("data") or {}).get("short_url")
        )
        if not short_url:
            return ToolResult(
                success=False,
                error="weiyun.gen_share_link returned no short_url",
                data=res,
            )

        return ToolResult(
            success=True,
            data={
                "short_url": short_url,
                "share_name": res.get("share_name") or share_name,
                "mcp_session_id": inputs.get("mcp_session_id"),
            },
            duration_seconds=elapsed,
        )
