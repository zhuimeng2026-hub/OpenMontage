"""Weiyun (腾讯微云) MCP-token share-link tool — PUBLISH tier.

Generates a shareable short URL for files/dirs already stored in Tencent Weiyun
via the official MCP endpoint, using the MCP token (``WEIYUN_MCP_TOKEN``). This
is the token-based counterpart to the cookie/mcporter-based
``weiyun.gen_share_link`` tool and requires NO ``mcporter`` CLI install.

It reuses the proven HTTP transport vendored into ``weiyun_upload_lib.mcp_call``
(the same endpoint, auth header, and retry logic that the upload tool uses), so
the only credential needed is the MCP token already configured in ``.env``.

Optional ``retain_days`` adds a server-side expiry tracker entry to
``projects/_share_expiry/index.jsonl``. A separate sweeper
(``tools/publishers/weiyun_expiry_sweep.py``) reads that index and calls
``weiyun_delete`` when ``retain_days`` elapses — the underlying MCP
``gen_share_link`` has no native expiration parameter, so this is the
documented workaround.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
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
from .weiyun_upload_lib import mcp_call


# Where we keep the expiry index. Sibling to projects/ — matches existing
# projects/.mcp_sessions, projects/.uploads, projects/.users pattern.
SHARE_EXPIRY_DIR = Path(__file__).resolve().parents[2] / "projects" / "_share_expiry"
SHARE_EXPIRY_INDEX = SHARE_EXPIRY_DIR / "index.jsonl"


def _append_expiry_entry(
    *,
    short_url: str,
    file_ids: list[str],
    pdir_keys: list[str],
    retain_days: int,
    project_id: Optional[str],
    share_name: str,
) -> None:
    """Append one row to projects/_share_expiry/index.jsonl (best-effort)."""
    try:
        SHARE_EXPIRY_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        entry = {
            "short_url":   short_url,
            "file_ids":    file_ids,
            "pdir_keys":   pdir_keys,
            "share_name":  share_name,
            "created_at":  now.isoformat(),
            "expires_at":  (now + timedelta(days=retain_days)).isoformat(),
            "retain_days": retain_days,
            "project_id":  project_id or "",
            "status":      "active",
            "deleted_at":  None,
        }
        with SHARE_EXPIRY_INDEX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - expiry tracking must never break the publish
        pass


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
            "retain_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": (
                    "Optional retention window in days. Weiyun MCP's gen_share_link has "
                    "no native expiration; setting this writes an entry to "
                    "projects/_share_expiry/index.jsonl and a sweeper deletes the "
                    "underlying file at expires_at, which invalidates the share."
                ),
            },
            "pdir_key": {
                "type": "string",
                "description": (
                    "Optional parent dir key. Required when retain_days is set so the "
                    "sweeper can call weiyun.delete (each entry needs pdir_key)."
                ),
            },
            "project_id": {
                "type": "string",
                "description": "Optional project id for grouping the expiry row.",
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
            "expires_at": {"type": "string", "description": "ISO timestamp when the share is queued for deletion. Empty if retain_days was unset."},
            "expiry_index": {"type": "string", "description": "Path to the expiry index row (relative)."},
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

        # Optional: register an expiry entry so the sweeper can revoke the share
        # by deleting the underlying file once `retain_days` elapses.
        retain_days = inputs.get("retain_days")
        expires_at_str = ""
        if isinstance(retain_days, int) and retain_days >= 1:
            pdir_key = (inputs.get("pdir_key") or "").strip()
            file_ids = [f for f in (inputs.get("file_list") or []) if f]
            _append_expiry_entry(
                short_url=short_url,
                file_ids=file_ids,
                pdir_keys=[pdir_key] if pdir_key else [],
                retain_days=retain_days,
                project_id=inputs.get("project_id"),
                share_name=res.get("share_name") or share_name,
            )
            expires_at_str = (
                datetime.now(timezone.utc) + timedelta(days=retain_days)
            ).isoformat()

        return ToolResult(
            success=True,
            data={
                "short_url": short_url,
                "share_name": res.get("share_name") or share_name,
                "mcp_session_id": inputs.get("mcp_session_id"),
                "expires_at": expires_at_str,
                "expiry_index": str(SHARE_EXPIRY_INDEX.relative_to(Path(__file__).resolve().parents[2])) if expires_at_str else "",
            },
            duration_seconds=elapsed,
        )
