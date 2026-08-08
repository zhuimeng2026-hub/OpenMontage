"""MCP wrappers for Tencent Weiyun cloud storage.

Provides 12 tool wrappers that delegate to the local `mcporter` CLI with the
`weiyun` server profile.  The mcporter profile must be configured before use:

    # Set the token once (run after installing the weiyun skill):
    bash /root/.claude/skills/weiyun/setup.sh weiyun_set_token <token>

or via the dedicated env var (takes precedence over mcporter config):

    export WEIYUN_MCP_TOKEN=...

Tools exposed:

    weiyun.list              — query a directory (files + subdirs)
    weiyun.list_by_category  — list files by type (doc/image/video/…)
    weiyun.download          — fetch HTTPS download links
    weiyun.delete            — move to recycle bin or permanently delete
    weiyun.upload            — two-phase FTN upload (use scripts/upload_to_weiyun.py instead)
    weiyun.gen_share_link    — create a shareable short URL
    weiyun.rename_file       — rename a file
    weiyun.rename_dir        — rename a directory
    weiyun.create_dir        — create a new folder
    weiyun.move_file         — move a file to another directory
    weiyun.move_dir          — move a directory to another location
    check_skill_update       — check if the weiyun skill is up to date
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier
from tools.tool_registry import registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcporter_path() -> str | None:
    """Return the path to the mcporter binary, or None."""
    return shutil.which("mcporter")


def _run_mcporter(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run mcporter and return (returncode, stdout, stderr)."""
    cmd = ["mcporter", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"mcporter timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "mcporter not found — install via: npm install -g mcporter@0.8.1"


def _parse_json_output(stdout: str) -> Any:
    """Parse mcporter JSON output, stripping any leading noise."""
    stdout = stdout.strip()
    if not stdout:
        return {}
    # mcporter may emit a leading JSON object; find the first '{'
    start = stdout.find("{")
    if start == -1:
        start = stdout.find("[")
    if start == -1:
        return {}
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return {"raw": stdout}


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class WeiyunList(BaseTool):
    name = "weiyun.list"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = []
    best_for = ["listing Weiyun cloud storage contents"]
    not_good_for = ["offline use"]
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Number of items to return (max 50)"},
            "offset": {"type": "integer", "description": "Pagination offset"},
            "dir_key": {"type": "string", "description": "Directory key (hex)"},
            "pdir_key": {"type": "string", "description": "Parent directory key"},
            "order_by": {"type": "integer", "description": "Sort: 0=none, 1=name, 2=mtime"},
            "asc": {"type": "boolean", "description": "Ascending order"},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "dir_list": {"type": "array"},
            "file_list": {"type": "array"},
            "pdir_key": {"type": "string"},
            "finish_flag": {"type": "boolean"},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.list", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunListByCategory(BaseTool):
    name = "weiyun.list_by_category"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = []
    best_for = ["browsing Weiyun by file type"]
    not_good_for = ["offline use"]
    input_schema = {
        "type": "object",
        "properties": {
            "category_id": {"type": "integer", "description": "Category: 1=doc, 2=excel, 4=ppt, 8=pdf, 64=image, 4095=all"},
            "lib_id": {"type": "integer", "description": "Library: 1=docs, 2=images, 3=music, 4=video"},
            "count": {"type": "integer", "description": "Number of items"},
            "local_version": {"type": "string", "description": "Pagination cursor"},
        },
    }
    output_schema = {"type": "object", "properties": {"file_list": {"type": "array"}, "server_version": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.list_by_category", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunDownload(BaseTool):
    name = "weiyun.download"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = []
    best_for = ["getting download links for Weiyun files"]
    not_good_for = ["downloading shared links (only own files)"]
    input_schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"file_id": {"type": "string"}, "pdir_key": {"type": "string"}}},
            },
        },
    }
    output_schema = {"type": "object", "properties": {"items": {"type": "array"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.download", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunDelete(BaseTool):
    name = "weiyun.delete"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["deletes files/directories from Weiyun"]
    best_for = ["cleaning up Weiyun storage"]
    not_good_for = ["recovering deleted files (use recycle bin)"]
    input_schema = {
        "type": "object",
        "properties": {
            "file_list": {"type": "array", "description": "Files to delete"},
            "dir_list": {"type": "array", "description": "Directories to delete"},
            "delete_completely": {"type": "boolean", "description": "True for permanent delete, false for recycle bin"},
        },
    }
    output_schema = {"type": "object", "properties": {"freed_space": {"type": "integer"}, "freed_index_cnt": {"type": "integer"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.delete", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunUpload(BaseTool):
    name = "weiyun.upload"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["uploads file to Weiyun"]
    best_for = ["uploading files via MCP (use upload_to_weiyun.py for production)"]
    not_good_for = ["large files (use the dedicated script instead)"]
    input_schema = {
        "type": "object",
        "required": ["filename", "file_size", "file_sha", "block_sha_list"],
        "properties": {
            "filename": {"type": "string"},
            "file_size": {"type": "integer"},
            "file_sha": {"type": "string"},
            "block_sha_list": {"type": "array", "items": {"type": "string"}},
            "check_sha": {"type": "string"},
            "check_data": {"type": "string"},
            "pdir_key": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"file_exist": {"type": "boolean"}, "upload_key": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.upload", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunGenShareLink(BaseTool):
    name = "weiyun.gen_share_link"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["creates share link in Weiyun"]
    best_for = ["generating shareable links for Weiyun files"]
    not_good_for = ["downloading shared links (only own files)"]
    input_schema = {
        "type": "object",
        "properties": {
            "file_list": {"type": "array"},
            "dir_list": {"type": "array"},
            "share_name": {"type": "string"},
            "passwd": {"type": "string", "description": "6-char share password"},
        },
    }
    output_schema = {"type": "object", "properties": {"short_url": {"type": "string"}, "share_name": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.gen_share_link", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunRenameFile(BaseTool):
    name = "weiyun.rename_file"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["renames file in Weiyun"]
    best_for = ["renaming Weiyun files"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["file_id", "pdir_key", "new_filename"],
        "properties": {
            "file_id": {"type": "string"},
            "pdir_key": {"type": "string"},
            "new_filename": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"error": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.rename_file", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunRenameDir(BaseTool):
    name = "weiyun.rename_dir"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["renames directory in Weiyun"]
    best_for = ["renaming Weiyun directories"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["dir_key", "pdir_key", "new_dir_name", "src_dir_name"],
        "properties": {
            "dir_key": {"type": "string"},
            "pdir_key": {"type": "string"},
            "new_dir_name": {"type": "string"},
            "src_dir_name": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"error": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.rename_dir", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunCreateDir(BaseTool):
    name = "weiyun.create_dir"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["creates directory in Weiyun"]
    best_for = ["creating folders in Weiyun"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["dir_name"],
        "properties": {
            "pdir_key": {"type": "string", "description": "Parent directory key"},
            "dir_name": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"dir_key": {"type": "string"}, "dir_name": {"type": "string"}, "error": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.create_dir", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunMoveFile(BaseTool):
    name = "weiyun.move_file"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["moves file in Weiyun"]
    best_for = ["reorganizing Weiyun files"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["file_id", "src_pdir_key", "dst_pdir_key"],
        "properties": {
            "file_id": {"type": "string"},
            "src_pdir_key": {"type": "string"},
            "dst_pdir_key": {"type": "string"},
            "filename": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"error": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.move_file", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class WeiyunMoveDir(BaseTool):
    name = "weiyun.move_dir"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = ["moves directory in Weiyun"]
    best_for = ["reorganizing Weiyun folders"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["dir_key", "src_pdir_key", "dst_pdir_key"],
        "properties": {
            "dir_key": {"type": "string"},
            "src_pdir_key": {"type": "string"},
            "dst_pdir_key": {"type": "string"},
            "dir_name": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "properties": {"error": {"type": "string"}}}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("weiyun.move_dir", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


class CheckSkillUpdate(BaseTool):
    name = "check_skill_update"
    version = "1.0.8"
    tier = ToolTier.PUBLISH
    capability = "cloud_storage"
    provider = "tencent_weiyun"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    dependencies = ["cmd:mcporter"]
    install_instructions = "Install mcporter: npm install -g mcporter@0.8.1"
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)
    side_effects = []
    best_for = ["checking if weiyun skill needs updating"]
    not_good_for = []
    input_schema = {
        "type": "object",
        "required": ["version"],
        "properties": {
            "version": {"type": "string", "description": "Current skill version (e.g. '1.0.8')"},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "latest": {"type": "string"},
            "release_note": {"type": "string"},
            "instruction": {"type": "string"},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return self._call_mcporter("check_skill_update", inputs)

    def _call_mcporter(self, tool_name: str, inputs: dict[str, Any]) -> ToolResult:
        mcporter = _mcporter_path()
        if not mcporter:
            return ToolResult(success=False, error="mcporter not found. Install with: npm install -g mcporter@0.8.1")

        args = ["call", "--server", "weiyun", "--tool", tool_name, "--output", "json"]
        if inputs:
            args.extend(["--args", json.dumps(inputs, ensure_ascii=False)])

        returncode, stdout, stderr = _run_mcporter(args)

        if returncode != 0:
            error_msg = stderr.strip() or stdout.strip()
            if "token" in error_msg.lower() or "auth" in error_msg.lower() or "401" in error_msg:
                error_msg = "Weiyun authentication failed. Check WEIYUN_MCP_TOKEN or run setup.sh"
            return ToolResult(success=False, error=error_msg)

        try:
            result = _parse_json_output(stdout)
            if result.get("error") or result.get("code"):
                return ToolResult(
                    success=False,
                    error=f"weiyun error: {result.get('error') or result.get('message', result.get('code'))}",
                    data=result,
                )
            return ToolResult(success=True, data=result)
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to parse response: {exc}")


# ---------------------------------------------------------------------------
# Register tools
# ---------------------------------------------------------------------------


def _register_weiyun_tools() -> list[str]:
    """Register all weiyun tools with the registry."""
    tools = [
        WeiyunList(),
        WeiyunListByCategory(),
        WeiyunDownload(),
        WeiyunDelete(),
        WeiyunUpload(),
        WeiyunGenShareLink(),
        WeiyunRenameFile(),
        WeiyunRenameDir(),
        WeiyunCreateDir(),
        WeiyunMoveFile(),
        WeiyunMoveDir(),
        CheckSkillUpdate(),
    ]
    registered = []
    for tool in tools:
        registry.register(tool)
        registered.append(tool.name)
    return registered


# Register on import
_registered_weiyun = _register_weiyun_tools()
print(f"[mcp_server] Registered {len(_registered_weiyun)} weiyun tools: {', '.join(_registered_weiyun)}", file=sys.stderr)
