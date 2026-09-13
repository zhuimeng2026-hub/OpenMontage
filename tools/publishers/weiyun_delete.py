"""Weiyun (腾讯微云) MCP-token delete tool — PUBLISH tier.

Deletes a batch of files or directories from Tencent Weiyun via the official
MCP endpoint, using the MCP token (``WEIYUN_MCP_TOKEN``). Token-based; no
QR-code login or cookies required.

This is the inverse of ``weiyun_upload`` and the enabler of "expire a share"
flows: since the upstream ``weiyun.gen_share_link`` MCP has no expiration
parameter, OpenMontage simulates retention by deleting the underlying file
when ``retain_days`` elapses (see ``weiyun_expiry_sweep.py``).
"""

from __future__ import annotations

import os
import time
from typing import Any

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


class WeiyunDelete(BaseTool):
    name = "weiyun_delete"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "weiyun"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["env:WEIYUN_MCP_TOKEN"]
    install_instructions = (
        "Set WEIYUN_MCP_TOKEN in the OpenMontage `.env` (the same token the "
        "Tencent Weiyun MCP skill uses). No QR-code login or cookies. "
        "Also ensure `requests` is installed (pip install requests)."
    )

    capabilities = ["delete_weiyun_files"]
    supports = {
        "local_offline": False,
        "free": False,
        "uploads": False,
    }
    best_for = [
        "revoking a Weiyun share link by deleting the underlying file",
        "freeing space after a render is delivered to a customer",
        "expiry sweeps: bulk-deleting files whose retain window has elapsed",
    ]
    not_good_for = [
        "uploading files (use weiyun_upload)",
        "soft-cancel: this tool removes the file (or moves it to trash); the share URL stays valid until the file is gone",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "file_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_id":  {"type": "string"},
                        "pdir_key": {"type": "string"},
                    },
                    "required": ["file_id", "pdir_key"],
                },
                "description": "Files to delete. Each entry needs file_id and pdir_key (the dir key from weiyun.list).",
            },
            "dir_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dir_key":  {"type": "string"},
                        "dir_name": {"type": "string"},
                        "pdir_key": {"type": "string"},
                    },
                    "required": ["dir_key", "dir_name", "pdir_key"],
                },
                "description": "Directories to delete. Each entry needs dir_key, dir_name and pdir_key.",
            },
            "delete_completely": {
                "type": "boolean",
                "default": False,
                "description": "true = permanently delete (irreversible); false = move to Weiyun trash (recoverable). Default false.",
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
            "freed_index_cnt": {"type": "integer", "description": "Number of files + directories removed."},
            "freed_space":     {"type": "integer", "description": "Bytes freed."},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True
    )
    side_effects = [
        "deletes files or directories from the operator's Tencent Weiyun account",
    ]
    user_visible_verification = [
        "Open Weiyun and confirm the file no longer appears (or sits in Trash)",
        "Open the previously-issued share URL — it should now 404",
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
        super().check_dependencies()
        try:
            import requests  # noqa: F401
        except ImportError:
            raise DependencyError(
                "Python package 'requests' is not installed. pip install requests"
            )

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        file_list = inputs.get("file_list") or []
        dir_list  = inputs.get("dir_list")  or []
        delete_completely = bool(inputs.get("delete_completely", False))

        if not file_list and not dir_list:
            return ToolResult(
                success=False,
                error="file_list or dir_list is required to delete anything",
            )

        # Shape the entries to match the MCP schema exactly.
        for entry in file_list:
            if not entry.get("file_id") or not entry.get("pdir_key"):
                return ToolResult(
                    success=False,
                    error="each file_list entry needs both file_id and pdir_key",
                )
        for entry in dir_list:
            if not entry.get("dir_key") or not entry.get("dir_name") or not entry.get("pdir_key"):
                return ToolResult(
                    success=False,
                    error="each dir_list entry needs dir_key, dir_name, and pdir_key",
                )

        try:
            headers = self._build_headers()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        mcp_url = self._mcp_url()
        args: dict[str, Any] = {"delete_completely": delete_completely}
        if file_list:
            args["file_list"] = [
                {"file_id": e["file_id"], "pdir_key": e["pdir_key"]} for e in file_list
            ]
        if dir_list:
            args["dir_list"] = [
                {"dir_key": e["dir_key"], "dir_name": e["dir_name"], "pdir_key": e["pdir_key"]}
                for e in dir_list
            ]

        started = time.time()
        try:
            res = mcp_call(mcp_url, headers, "weiyun.delete", args)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"Weiyun delete failed: {exc}")
        elapsed = round(time.time() - started, 2)

        if res.get("error"):
            return ToolResult(
                success=False,
                error=f"weiyun.delete error: {res.get('error')}",
                data=res,
                duration_seconds=elapsed,
            )

        return ToolResult(
            success=True,
            data={
                "freed_index_cnt": int(res.get("freed_index_cnt", 0) or 0),
                "freed_space":     int(res.get("freed_space", 0) or 0),
                "delete_completely": delete_completely,
                "mcp_session_id":  inputs.get("mcp_session_id"),
            },
            duration_seconds=elapsed,
        )