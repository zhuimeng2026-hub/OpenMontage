"""Upload a generated artifact to a public server over SSH/rsync.

All connection settings come from the OpenMontage ``.env`` file.  The tool
does not invoke a shell, so paths and credentials are not interpreted by a
command shell.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required in .env")
    return value


class RsyncUpload(BaseTool):
    """Upload a local generated file to a configured SSH/rsync server."""

    name = "rsync_upload_artifact"
    version = "1.0.0"
    tier = ToolTier.PUBLISH
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "artifact_delivery"
    provider = "rsync"
    capabilities = ["artifact_upload", "ssh_transfer", "public_download_delivery"]
    dependencies = ["cmd:rsync", "cmd:ssh"]
    install_instructions = "Install rsync and OpenSSH client, then configure RSYNC_* in .env."
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, disk_mb=64, network_required=True)
    side_effects = ["uploads a configured local file to the remote SSH server"]
    best_for = ["delivering rendered videos to a controlled public HTTPS server"]
    not_good_for = ["high-volume CDN distribution; use object storage instead"]
    input_schema = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string", "description": "Absolute local file path; defaults to RSYNC_SOURCE_PATH."},
            "remote_name": {"type": "string", "description": "Optional remote filename; defaults to the source filename."},
            "dry_run": {"type": "boolean", "default": False},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string"},
            "remote_path": {"type": "string"},
            "download_url": {"type": "string"},
            "bytes": {"type": "integer"},
        },
    }

    @staticmethod
    def _port() -> int:
        try:
            port = int(os.environ.get("RSYNC_SSH_PORT", "22"))
        except ValueError as exc:
            raise ValueError("RSYNC_SSH_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("RSYNC_SSH_PORT must be between 1 and 65535")
        return port

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            source = Path(str(inputs.get("source_path") or _required("RSYNC_SOURCE_PATH"))).expanduser()
            if not source.is_absolute():
                raise ValueError("source_path must be an absolute path")
            source = source.resolve()
            if not source.is_file():
                raise ValueError(f"source file does not exist: {source}")

            key_path = Path(_required("RSYNC_SSH_KEY_PATH")).expanduser().resolve()
            if not key_path.is_file():
                raise ValueError(f"SSH key file does not exist: {key_path}")
            host = _required("RSYNC_REMOTE_HOST")
            user = _required("RSYNC_REMOTE_USER")
            remote_dir = _required("RSYNC_REMOTE_PATH").rstrip("/")
            remote_name = str(inputs.get("remote_name") or source.name)
            if not remote_name or "/" in remote_name or "\\" in remote_name or remote_name in {".", ".."}:
                raise ValueError("remote_name must be a plain filename")

            remote_path = f"{user}@{host}:{remote_dir}/{remote_name}"
            ssh_command = [
                os.environ.get("RSYNC_SSH_COMMAND", "ssh"),
                "-i", str(key_path),
                "-p", str(self._port()),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
            ]
            known_hosts = os.environ.get("RSYNC_KNOWN_HOSTS_PATH", "").strip()
            if known_hosts:
                known_hosts_path = Path(known_hosts).expanduser().resolve()
                if not known_hosts_path.is_file():
                    raise ValueError(f"known_hosts file does not exist: {known_hosts_path}")
                ssh_command.extend(["-o", f"UserKnownHostsFile={known_hosts_path}"])
            command = [
                os.environ.get("RSYNC_COMMAND", "rsync"), "-avP",
                "--chmod=F644,D755", "-e", shlex.join(ssh_command),
                str(source), remote_path,
            ]
            if inputs.get("dry_run"):
                return ToolResult(success=True, data={"command": command, "remote_path": remote_path}, duration_seconds=time.monotonic() - started)

            timeout = int(os.environ.get("RSYNC_TIMEOUT_SECONDS", "1800"))
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "rsync failed").strip()[-2000:]
                return ToolResult(success=False, error=f"rsync exited {completed.returncode}: {detail}", duration_seconds=time.monotonic() - started)

            base_url = os.environ.get("RSYNC_PUBLIC_BASE_URL", "").strip().rstrip("/")
            result = {
                "source_path": str(source),
                "remote_path": remote_path,
                "bytes": source.stat().st_size,
                "stdout": completed.stdout[-2000:],
            }
            if base_url:
                from urllib.parse import quote
                result["download_url"] = f"{base_url}/{quote(remote_name)}"
            return ToolResult(success=True, data=result, artifacts=[str(source)], duration_seconds=time.monotonic() - started)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=time.monotonic() - started)
