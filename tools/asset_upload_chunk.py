"""Resumable chunked asset uploads for remote MCP clients.

Use start -> append (one or more times) -> complete. This keeps each JSON-RPC
request small enough for common reverse proxies while preserving a SHA-256
verified final asset.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from tools.asset_upload import _ALLOWED_EXTENSIONS, _SAFE_FILENAME, _max_upload_bytes
from lib.workbuddy_session import register_image, require_session
from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier


class UploadAssetChunk(BaseTool):
    name = "upload_asset_chunk"
    version = "1.0.0"
    tier = ToolTier.CORE
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "asset_management"
    provider = "openmontage"
    capabilities = ["asset_upload", "resumable_upload", "batch_upload"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=100)
    side_effects = ["writes upload state and media below projects/<project_id>/assets"]
    input_schema = {
        "type": "object", "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["start", "append", "complete"]},
            "project_id": {"type": "string"}, "filename": {"type": "string"},
            "total_bytes": {"type": "integer", "minimum": 1},
            "mime_type": {"type": "string"}, "sha256": {"type": "string"},
            "upload_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
            "chunk_base64": {"type": "string"},
        },
    }

    @staticmethod
    def _root() -> Path:
        return (Path(__file__).resolve().parent.parent / "projects").resolve()

    @classmethod
    def _state_paths(cls, upload_id: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ValueError("invalid upload_id")
        state_dir = cls._root() / ".uploads"
        return state_dir / f"{upload_id}.json", state_dir / f"{upload_id}.part"

    @staticmethod
    def _decode_chunk(value: Any) -> bytes:
        if not isinstance(value, str) or not value:
            raise ValueError("chunk_base64 is required")
        try:
            return base64.b64decode(re.sub(r"\s+", "", value), validate=True)
        except Exception as exc:
            raise ValueError("chunk_base64 is not valid base64") from exc

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            operation = inputs.get("operation")
            root = self._root()
            root.mkdir(parents=True, exist_ok=True)
            if operation == "start":
                project_id, filename = inputs.get("project_id"), inputs.get("filename")
                if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id):
                    raise ValueError("project_id must be a safe basename")
                if not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename):
                    raise ValueError("filename must be a safe basename")
                if Path(filename).suffix.lower() not in _ALLOWED_EXTENSIONS:
                    raise ValueError("unsupported media extension")
                total = int(inputs.get("total_bytes", 0))
                if total < 1 or total > _max_upload_bytes():
                    raise ValueError(f"total_bytes must be between 1 and {_max_upload_bytes()} bytes")
                upload_id = uuid.uuid4().hex
                state_path, part_path = self._state_paths(upload_id)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                session_id = inputs.get("mcp_session_id")
                session_digest = require_session(session_id)
                state_path.write_text(json.dumps({"project_id": project_id, "filename": filename, "total_bytes": total, "mime_type": inputs.get("mime_type"), "sha256": inputs.get("sha256"), "session_hash": session_digest, "created": time.time()}), encoding="utf-8")
                part_path.write_bytes(b"")
                return ToolResult(True, {"upload_id": upload_id, "next_offset": 0, "chunk_limit_bytes": min(1024 * 1024, _max_upload_bytes())}, duration_seconds=time.monotonic()-started)

            upload_id = inputs.get("upload_id")
            state_path, part_path = self._state_paths(upload_id)
            if not state_path.is_file() or not part_path.is_file():
                raise ValueError("upload_id not found or expired")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            current_session = inputs.get("mcp_session_id")
            current_session_hash = require_session(current_session)
            if state.get("session_hash") != current_session_hash:
                raise ValueError("upload_id belongs to a different MCP session")
            if operation == "append":
                chunk = self._decode_chunk(inputs.get("chunk_base64"))
                offset = int(inputs.get("offset", -1))
                current = part_path.stat().st_size
                if offset != current:
                    raise ValueError(f"offset mismatch; expected {current}")
                if current + len(chunk) > int(state["total_bytes"]):
                    raise ValueError("chunk exceeds declared total_bytes")
                with part_path.open("ab") as handle:
                    handle.write(chunk)
                return ToolResult(True, {"upload_id": upload_id, "next_offset": current + len(chunk), "complete": current + len(chunk) == int(state["total_bytes"])}, duration_seconds=time.monotonic()-started)

            if operation != "complete":
                raise ValueError("operation must be start, append, or complete")
            content_size = part_path.stat().st_size
            if content_size != int(state["total_bytes"]):
                raise ValueError(f"incomplete upload: {content_size}/{state['total_bytes']} bytes")
            digest = hashlib.sha256(part_path.read_bytes()).hexdigest()
            if state.get("sha256") and state["sha256"].lower() != digest:
                raise ValueError("sha256 does not match uploaded content")
            assets_dir = (root / state["project_id"] / "assets" / "_sessions" / state["session_hash"]).resolve()
            assets_dir.mkdir(parents=True, exist_ok=True)
            target = (assets_dir / state["filename"]).resolve()
            target.relative_to(assets_dir)
            if target.exists():
                raise ValueError("asset already exists; use a different filename")
            os.replace(part_path, target)
            mime_type = state.get("mime_type") or mimetypes.guess_type(state["filename"])[0] or "application/octet-stream"
            asset = {"id": f"{state['project_id']}-{digest[:12]}", "filename": state["filename"], "path": str(target), "relative_path": target.relative_to(root.parent).as_posix(), "type": "image" if mime_type.startswith("image/") else "video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else "media", "mime_type": mime_type, "bytes": content_size, "sha256": digest, "source": "mcp_chunked_upload", "session_hash": state["session_hash"]}
            batch = None
            if asset["type"] == "image":
                batch = register_image(current_session, state["project_id"], asset)
            state_path.unlink(missing_ok=True)
            return ToolResult(True, {"asset": asset, "asset_manifest": {"assets": [asset]}, "upload_id": upload_id, **({"batch": batch} if batch else {})}, [str(target)], duration_seconds=time.monotonic()-started)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return ToolResult(False, error=str(exc), duration_seconds=time.monotonic()-started)
