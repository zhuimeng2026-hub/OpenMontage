"""Resumable chunked asset uploads for remote MCP clients.

Use start -> append (one or more times) -> complete. This keeps each JSON-RPC
request small enough for common reverse proxies while preserving a SHA-256
verified final asset.

Phase C: paths are derived from ``ProjectWorkspace`` so chunked uploads
land under the authenticated principal's namespace
(``projects/users/<namespace_key>/.uploads/<upload_id>.json`` for state and
``projects/users/<namespace_key>/<project_id>/assets/_sessions/<digest>/``
for the final asset). The pre-Phase-C global ``projects/.uploads/`` shared
across users is gone — see
``docs/user-isolation-via-mcp-session.md`` §Phase C audit checklist.
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

from tools.asset_upload import UploadAsset, _ALLOWED_EXTENSIONS, _max_upload_bytes
from lib.workbuddy_session import register_asset, register_image, replace_asset_by_sha, require_session
from lib.project_workspace import ProjectWorkspace, WorkspaceErrorError
from lib.namespace_version import NamespaceVersion, NamespaceVersionError, current_namespace_version
from lib.principal_sanitize import sanitize_project_id
from lib import paths as lib_paths
from tools.base_tool import BaseTool, ResourceProfile, ToolResult, ToolRuntime, ToolStability, ToolTier

_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROJECT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

# Arguments each operation cannot work without. Checked before anything is
# written so a client that omits one gets told which one, instead of tripping
# over an unrelated downstream error.
_REQUIRED_BY_OPERATION: dict[str, tuple[str, ...]] = {
    "start": ("project_id", "filename", "total_bytes"),
    "append": ("upload_id", "offset", "chunk_base64"),
    "complete": ("upload_id",),
}


def _is_absent(value: Any) -> bool:
    """Treat None and blank strings as missing; 0 and False are valid values."""
    return value is None or (isinstance(value, str) and not value.strip())


class UploadAssetChunk(BaseTool):
    name = "upload_asset_chunk"
    version = "1.1.0"
    tier = ToolTier.CORE
    stability = ToolStability.PRODUCTION
    runtime = ToolRuntime.LOCAL
    capability = "asset_management"
    provider = "openmontage"
    capabilities = ["asset_upload", "resumable_upload", "batch_upload"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=100)
    side_effects = [
        "writes upload state under projects/users/<ns>/.uploads/",
        "writes final asset under projects/users/<ns>/<project_id>/assets/_sessions/<digest>/",
    ]
    input_schema = {
        "type": "object", "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["start", "append", "complete"]},
            "project_id": {"type": "string", "pattern": _PROJECT_ID_PATTERN,
                           "description": "Safe basename; required when operation=start."},
            "filename": {"type": "string",
                         "description": "Required when operation=start."},
            "total_bytes": {"type": "integer", "minimum": 1,
                            "description": "Required when operation=start."},
            "mime_type": {"type": "string"}, "sha256": {"type": "string"},
            "upload_id": {"type": "string",
                          "description": "Required when operation=append or complete."},
            "offset": {"type": "integer", "minimum": 0,
                       "description": "Required when operation=append."},
            "chunk_base64": {"type": "string",
                             "description": "Required when operation=append."},
        },
        # Conditional requirements: the flat "required" list above cannot
        # express "project_id only matters for start", but allOf/if/then can.
        "allOf": [
            {
                "if": {"properties": {"operation": {"const": op}}, "required": ["operation"]},
                "then": {"required": list(required)},
            }
            for op, required in _REQUIRED_BY_OPERATION.items()
        ],
    }

    @staticmethod
    def _state_paths(upload_id: str, project_id: str) -> tuple[Path, Path]:
        """Return (state_json_path, part_path) for ``upload_id`` under the
        per-principal ``.uploads/`` directory.

        Phase C: the upload-id namespace is now per-principal
        (``projects/users/<namespace_key>/.uploads/``), not global. A user
        who guesses another user's upload_id still cannot reach the file
        because the file does not exist in their namespace.

        ``project_id`` is only required so ``for_current_principal`` can
        hand back the right ``upload_state`` directory; the value is
        untrusted at this layer because the state file is keyed by
        upload_id, but ``for_current_principal`` runs the standard
        ``sanitize_project_id`` allow-list so a hostile caller cannot
        smuggle a path component in the project_id parameter.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise ValueError("invalid upload_id")
        workspace = ProjectWorkspace.for_current_principal(project_id)
        state_dir = workspace.upload_state
        return state_dir / f"{upload_id}.json", state_dir / f"{upload_id}.part"

    @staticmethod
    def _migrate_legacy_state(
        upload_id: str,
        workspace: ProjectWorkspace,
        session_digest: str,
    ) -> tuple[Path, Path] | None:
        """Safely migrate an in-flight pre-Phase-C global upload.

        The old ``projects/.uploads`` directory was shared, so it is never
        selected by pathname alone. Migration requires both files, valid JSON,
        an exact session-hash match, and (when present) a matching namespace.
        The files are copied into the authenticated principal's v2 state
        directory before legacy files are removed; a failed copy leaves the
        source untouched and the request fails closed.
        """
        legacy_dir = Path(lib_paths.PROJECTS_DIR).resolve() / ".uploads"
        legacy_state = legacy_dir / f"{upload_id}.json"
        legacy_part = legacy_dir / f"{upload_id}.part"
        if not legacy_state.exists() and not legacy_part.exists():
            return None
        if not legacy_state.is_file() or not legacy_part.is_file():
            raise ValueError("upload_id not found or expired")
        try:
            state = json.loads(legacy_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("upload_id not found or expired")
        if not isinstance(state, dict) or state.get("session_hash") != session_digest:
            raise ValueError("upload_id not found or expired")
        state_namespace = state.get("namespace_key")
        if state_namespace and state_namespace != workspace.principal.namespace_key:
            raise ValueError("upload_id not found or expired")
        if sanitize_project_id(state.get("project_id")) != state.get("project_id"):
            raise ValueError("upload_id not found or expired")

        target_state = workspace.upload_state / f"{upload_id}.json"
        target_part = workspace.upload_state / f"{upload_id}.part"
        workspace.upload_state.mkdir(parents=True, exist_ok=True)
        if target_state.exists() or target_part.exists():
            # A concurrent migration won the race; use only a complete pair.
            if target_state.is_file() and target_part.is_file():
                return target_state, target_part
            raise ValueError("upload_id not found or expired")
        state["namespace_key"] = workspace.principal.namespace_key
        state["legacy_migrated"] = True
        state["legacy_migrated_at"] = time.time()
        try:
            target_part.write_bytes(legacy_part.read_bytes())
            target_state.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            # Best effort cleanup of a partial destination; never touch a
            # pre-existing file and never expose a partially migrated state.
            for path in (target_state, target_part):
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
            raise ValueError("upload_id not found or expired") from exc
        # Source cleanup is deliberately best-effort. Once the complete v2
        # pair exists, retaining a stale legacy copy is safer than deleting
        # the only copy because a Windows handle is still open.
        for path in (legacy_part, legacy_state):
            try:
                path.unlink()
            except OSError:
                pass
        return target_state, target_part

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
            if operation not in _REQUIRED_BY_OPERATION:
                raise ValueError("operation must be start, append, or complete")
            required = _REQUIRED_BY_OPERATION[operation]
            missing = [name for name in required if _is_absent(inputs.get(name))]
            if missing:
                raise ValueError(
                    f"operation={operation} is missing required argument(s): {', '.join(missing)}. "
                    f"Required for operation={operation}: {', '.join(required)}."
                )
            if operation == "start":
                project_id, original_filename = inputs.get("project_id"), inputs.get("filename")
                if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
                    raise ValueError(
                        "project_id must be a safe basename: 1-128 chars, start with a letter "
                        "or digit, then letters, digits, '.', '_' or '-' only (e.g. 'mclaw-demo')"
                    )
                filename, original_filename = UploadAsset._sanitize_filename(original_filename)
                if Path(filename).suffix.lower() not in _ALLOWED_EXTENSIONS:
                    raise ValueError("unsupported media extension")
                total = int(inputs.get("total_bytes", 0))
                if total < 1 or total > _max_upload_bytes():
                    raise ValueError(f"total_bytes must be between 1 and {_max_upload_bytes()} bytes")
                # Phase C: validate project_id via the workspace factory so
                # the file lands under the current principal's namespace.
                # ``for_current_principal`` raises ``PrincipalNotFound`` if
                # no MCP session is bound (no header, no Phase B registry
                # entry) — that propagates untouched and surfaces as the
                # tool's failure mode.
                workspace = ProjectWorkspace.for_current_principal(project_id)
                upload_id = uuid.uuid4().hex
                state_path, part_path = self._state_paths(upload_id, project_id)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                session_id = inputs.get("mcp_session_id")
                session_digest = require_session(session_id)
                state_path.write_text(json.dumps({"project_id": workspace.project_id, "filename": filename, "safe_filename": filename, "original_filename": original_filename, "total_bytes": total, "mime_type": inputs.get("mime_type"), "sha256": inputs.get("sha256"), "namespace_key": workspace.principal.namespace_key, "session_hash": session_digest, "created": time.time()}), encoding="utf-8")
                part_path.write_bytes(b"")
                return ToolResult(True, {"upload_id": upload_id, "next_offset": 0, "chunk_limit_bytes": min(1024 * 1024, _max_upload_bytes()), "filename": filename, "safe_filename": filename, "original_filename": original_filename, "renamed": filename != original_filename}, duration_seconds=time.monotonic()-started)

            # append/complete path: look up state by upload_id. We do not
            # know project_id from the input, so the lookup must succeed
            # for the CURRENT principal regardless of which project the
            # upload was started for. We try a one-shot fallback: peek at
            # the cached namespace_key in the state file, compare to the
            # current principal's namespace_key, and bail with the same
            # "not found or expired" error if they differ. This means an
            # attacker who guesses a victim user's upload_id still cannot
            # tell whether the id exists.
            upload_id = inputs.get("upload_id")
            if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                raise ValueError("invalid upload_id")
            current_principal = ProjectWorkspace.for_current_principal  # noqa: F841 — forces PrincipalNotFound to surface early
            # Resolve the current principal's upload_state directory.
            # We use a placeholder project_id to get at the workspace
            # object; sanitize_project_id requires a real id, so we use
            # "lookup" — a dummy id we know is valid. We never use the
            # resulting root/assets paths because the state lookup below
            # reads the stored project_id from disk.
            _lookup_workspace = ProjectWorkspace.for_current_principal("lookup")
            upload_state_dir = _lookup_workspace.upload_state
            state_path = upload_state_dir / f"{upload_id}.json"
            part_path = upload_state_dir / f"{upload_id}.part"
            current_session = inputs.get("mcp_session_id")
            current_session_hash = require_session(current_session)
            if not state_path.is_file() or not part_path.is_file():
                migrated = self._migrate_legacy_state(
                    upload_id, _lookup_workspace, current_session_hash
                )
                if migrated is None:
                    raise ValueError("upload_id not found or expired")
                state_path, part_path = migrated
            state = json.loads(state_path.read_text(encoding="utf-8"))
            # Defense in depth: verify the cached namespace_key matches
            # the binding we just resolved. If a forged state file (or a
            # legacy pre-Phase-C file) snuck through with a different
            # namespace_key, refuse the request. Note that
            # ``for_current_principal`` already raised
            # ``PrincipalNotFound`` above if the session has no binding.
            state_ns = state.get("namespace_key")
            # Phase D feature flag — strict-mode enforcement.
            #
            # ``v2-only`` and the v2-bucket half of ``canary`` reject any
            # state file that does not carry a matching namespace_key:
            # a missing field means the file was written by a pre-Phase-C
            # chunked upload (no namespace_key was captured at start time)
            # and accepting it would land the final asset under the wrong
            # user's tree. We raise ``NamespaceVersionError`` so the
            # operator log distinguishes "policy refused" from "input
            # malformed" — the chunk tool collapses it to the same opaque
            # "upload_id not found or expired" string before returning to
            # the caller.
            #
            # ``legacy`` mode stays backward-compatible: a missing
            # ``namespace_key`` short-circuits the comparison the same way
            # the pre-Phase-D code did, so an unmigrated deployment's
            # chunked uploads keep working. Canary writes are v2 for every
            # bucket and therefore use the strict branch above.
            active_version = _lookup_workspace.mode
            active_version_value = getattr(active_version, "value", active_version)
            v2_strict = active_version_value in {
                NamespaceVersion.V2_ONLY.value,
                NamespaceVersion.CANARY.value,
            }
            if v2_strict and not state_ns:
                raise NamespaceVersionError(
                    "upload state file has no namespace_key but the active "
                    f"namespace mode is {active_version_value}; refusing to "
                    "complete a pre-Phase-C chunked upload"
                )
            if state_ns and state_ns != _lookup_workspace.principal.namespace_key:
                raise ValueError("upload_id not found or expired")
            if state.get("session_hash") != current_session_hash:
                raise ValueError("upload_id belongs to a different MCP session")
            # Re-derive the destination workspace using the project_id
            # stored in the state file. The state file's project_id
            # already passed sanitize_project_id when ``start`` wrote it,
            # so any ValueError here means the state file is corrupt or
            # tampered with — refuse with the same opaque error so we do
            # not leak its existence to the wrong principal.
            state_project_id = state.get("project_id")
            try:
                dest_workspace = ProjectWorkspace.for_current_principal(state_project_id)
            except (ValueError, WorkspaceErrorError):
                raise ValueError("upload_id not found or expired")
            # Repo root for the final ``relative_path`` (the caller-side
            # consumer of the asset record keys everything off the repo
            # root, so the relative_path must be repo-root-relative).
            # Reading via ``lib_paths.PROJECTS_DIR.parent`` keeps the path
            # computation in lock-step with the workspace factory — a
            # monkey-patched ``PROJECTS_DIR`` is observed here too.
            repo_root = Path(lib_paths.PROJECTS_DIR).resolve().parent
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
            # Final landing path: per-principal workspace's assets
            # dir + session-digested sub-folder. Phase C layout is
            # identical to single-shot upload.
            assets_root = dest_workspace.assets
            assets_dir = (assets_root / "_sessions" / state["session_hash"]).resolve()
            assets_dir.mkdir(parents=True, exist_ok=True)
            target = (assets_dir / state["filename"]).resolve()
            try:
                target.relative_to(assets_dir)
            except ValueError as exc:
                raise ValueError("filename escapes the project assets directory") from exc
            mime_type = state.get("mime_type") or mimetypes.guess_type(state["filename"])[0] or "application/octet-stream"
            asset = {"id": f"{state['project_id']}-{digest[:12]}", "filename": state["filename"], "original_filename": state.get("original_filename", state["filename"]), "relative_path": target.relative_to(repo_root).as_posix(), "type": "image" if mime_type.startswith("image/") else "video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else "media", "mime_type": mime_type, "bytes": content_size, "sha256": digest, "source": "mcp_chunked_upload", "session_hash": state["session_hash"]}

            if target.exists():
                existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if existing_digest != digest:
                    raise ValueError("asset already exists; use a different filename")
                batch = None
                canonical_asset = asset
                if asset["type"] == "image":
                    batch = register_image(current_session, state["project_id"], asset)
                    canonical_asset = next((item for item in batch.get("assets", []) if item.get("sha256") == digest), asset)
                part_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                canonical_target = (repo_root / canonical_asset["relative_path"]).resolve()
                return ToolResult(True, {"asset": canonical_asset, "asset_manifest": {"assets": [canonical_asset]}, "upload_id": upload_id, "deduplicated": True, **({"batch": batch} if batch else {})}, [str(canonical_target)], duration_seconds=time.monotonic()-started)

            os.replace(part_path, target)
            batch = None
            deduplicated = False
            canonical_asset = asset
            if asset["type"] == "image":
                batch = register_image(current_session, state["project_id"], asset)
                canonical_asset = next((item for item in batch.get("assets", []) if item.get("sha256") == digest), asset)
                deduplicated = canonical_asset.get("relative_path") != asset["relative_path"]
                if deduplicated:
                    # Before deleting the file we just moved, verify that the
                    # canonical asset it would shadow is actually present on
                    # disk AND still has the recorded sha256. If the canonical
                    # file vanished (cleanup job / RepoRoot mismatch / earlier
                    # race), our copy IS the new truth: promote it into the
                    # same sha slot via ``replace_asset_by_sha`` so the SPA
                    # never serves a 404 for a file the session thinks exists.
                    # Both functions hold the cross-process flock on the same
                    # session digest, so the read-modify-write below is
                    # serialized against any other worker.
                    canonical_target = (repo_root / canonical_asset["relative_path"]).resolve()
                    promote_self = True
                    if canonical_target.exists() and canonical_target.is_file():
                        try:
                            existing_digest = hashlib.sha256(canonical_target.read_bytes()).hexdigest()
                            if existing_digest == canonical_asset.get("sha256"):
                                promote_self = False
                        except OSError:
                            promote_self = True
                    if promote_self:
                        promoted = replace_asset_by_sha(current_session, state["project_id"], asset)
                        if promoted is not None:
                            batch = promoted
                            canonical_asset = asset
                            deduplicated = False
                        else:
                            # No matching sha entry found — fall through to
                            # safe behavior of unlinking our copy rather than
                            # corrupting state. The caller can retry with a
                            # fresh upload.
                            target.unlink(missing_ok=True)
                            deduplicated = True
                    else:
                        target.unlink(missing_ok=True)
            else:
                # Video/audio uploads are not part of the photo-render batch,
                # but they still have to be discoverable by asset id so the
                # caption / cloned-voice workflows can resolve them later.
                register_asset(current_session, state["project_id"], asset)
            state_path.unlink(missing_ok=True)
            canonical_target = (repo_root / canonical_asset["relative_path"]).resolve()
            return ToolResult(True, {"asset": canonical_asset, "asset_manifest": {"assets": [canonical_asset]}, "upload_id": upload_id, "deduplicated": deduplicated, **({"batch": batch} if batch else {})}, [str(canonical_target)], duration_seconds=time.monotonic()-started)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return ToolResult(False, error=str(exc), duration_seconds=time.monotonic()-started)
