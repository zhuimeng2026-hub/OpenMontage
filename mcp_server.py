"""OpenMontage MCP Server — exposes tools, pipelines, and checkpoints over MCP.

Run with streamable-http (for remote access):
    python mcp_server.py

Run with stdio (for local agent integration):
    python mcp_server.py stdio
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

# Ensure OpenMontage project root is on sys.path so tools/ and lib/ resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Change CWD so relative paths (pipeline/, projects/, output/) resolve correctly.
os.chdir(_PROJECT_ROOT)

# Logging setup (must be after _PROJECT_ROOT is defined)
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log = logging.getLogger("mcp_server")
_log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

# File handler with rotation (10 MB per file, keep 5 backups)
if not _log.handlers:
    _file_handler = RotatingFileHandler(
        _LOG_DIR / "mcp_server.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    _log.addHandler(_file_handler)

    # Stderr handler (for journal/systemd)
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(_formatter)
    _log.addHandler(_stderr_handler)

# Ensure OpenMontage project root is on sys.path so tools/ and lib/ resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Change CWD so relative paths (pipeline/, projects/, output/) resolve correctly.
os.chdir(_PROJECT_ROOT)

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from tools.tool_registry import registry
from tools.base_tool import ToolStatus
from lib import checkpoint as ckpt
from lib import pipeline_loader
from lib.mcp_session import (
    get_mcp_request_id,
    get_mcp_session_id,
    reset_mcp_request_id,
    reset_mcp_session_id,
    set_mcp_request_id,
    set_mcp_session_id,
)
from lib.workbuddy_session import begin_render, session_hash, update as update_session


_business_log = logging.getLogger("session_video")
_business_log.setLevel(logging.INFO)
_business_log.propagate = False
if not _business_log.handlers:
    _business_handler = RotatingFileHandler(
        _LOG_DIR / "session_video.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _business_handler.setFormatter(logging.Formatter("%(message)s"))
    _business_log.addHandler(_business_handler)


def _event(event: str, *, include_traceback: bool = False, **fields: Any) -> None:
    record = {"event": event, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    if include_traceback:
        record["traceback"] = traceback.format_exc()
    record = _safe_inputs(record)
    level = logging.ERROR if event == "workflow_failed" else logging.INFO
    _business_log.log(level, json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def _safe_inputs(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if key.lower() in {"content_base64", "chunk_base64", "token", "cookie", "authorization", "mcp_session_id"} else _safe_inputs(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_inputs(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", value)
        value = re.sub(
            r"(?i)((?:api[_-]?key|token|cookie|authorization)\s*[:=]\s*)[^\s,;]+",
            r"\1<redacted>",
            value,
        )
    return value

# ---------------------------------------------------------------------------
# Discover tools at import time
# ---------------------------------------------------------------------------
_discovered = registry.discover()
_AVAILABLE = sum(1 for _ in registry.get_available())
print(f"[mcp_server] Discovered {len(_discovered)} tools ({_AVAILABLE} available)", file=sys.stderr)

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "OpenMontage",
    host="::",
    port=8900,
    # Keep the MCP transport stateful so clients receive and reuse
    # Mcp-Session-Id.  The session id is also used to isolate uploads.
    stateless_http=False,
    json_response=True,
    instructions=(
        "OpenMontage agentic video production system. "
        "Use list_tools to discover available tools, execute_tool to run them, "
        "list_pipelines to see production workflows, and checkpoint tools to "
        "manage pipeline state. Every tool call returns a structured result "
        "with success, data, artifacts, cost_usd, and duration_seconds."
    ),
)

# ---------------------------------------------------------------------------
# Pydantic return models
# ---------------------------------------------------------------------------


class ToolSummary(BaseModel):
    name: str = Field(description="Tool name")
    capability: str = Field(description="What the tool does (e.g. tts, image_generation)")
    provider: str = Field(description="Service provider (e.g. elevenlabs, ffmpeg, fal)")
    status: str = Field(description="available | unavailable | degraded")
    tier: str = Field(description="core | voice | enhance | generate | source | analyze | publish")
    runtime: str = Field(description="local | local_gpu | api | hybrid")
    stability: str = Field(description="experimental | beta | production")


class ExecuteResult(BaseModel):
    success: bool = Field(description="Whether the tool execution succeeded")
    data: dict[str, Any] = Field(default_factory=dict, description="Tool output data")
    artifacts: list[str] = Field(default_factory=list, description="Output file paths")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    cost_usd: float = Field(default=0.0, description="Actual cost in USD")
    duration_seconds: float = Field(default=0.0, description="Execution time in seconds")
    seed: Optional[int] = Field(default=None, description="Random seed used")
    model: Optional[str] = Field(default=None, description="Model identifier used")


class DryRunResult(BaseModel):
    tool: str
    estimated_cost_usd: float
    estimated_runtime_seconds: float
    status: str
    would_execute: bool


class UploadAssetResult(BaseModel):
    success: bool = Field(description="Whether the asset was stored")
    asset: dict[str, Any] = Field(default_factory=dict, description="Asset manifest entry")
    asset_manifest: dict[str, Any] = Field(default_factory=dict, description="Manifest fragment ready for video tools")
    deduplicated: bool = Field(default=False, description="Whether an identical existing file was reused")
    status: Optional[str] = None
    asset_count: int = 0
    message: Optional[str] = None
    next_action: Optional[str] = None
    batch_id: Optional[str] = None
    error: Optional[str] = Field(default=None, description="Error message if upload failed")


class S3UploadResult(BaseModel):
    success: bool = Field(description="Whether the upload completed")
    url: Optional[str] = Field(default=None, description="Public or pre-signed download URL")
    object_key: Optional[str] = Field(default=None, description="S3 object key")
    bucket: Optional[str] = Field(default=None, description="Target bucket name")
    visibility: Optional[str] = Field(default=None, description="public or private")
    expires_at: Optional[str] = Field(default=None, description="ISO expiry for private URLs")
    download_page_url: Optional[str] = Field(default=None, description="HTML download page URL (when requested)")
    uploaded_files: list[dict[str, Any]] = Field(default_factory=list, description="Per-file upload records")
    publish_log: dict[str, Any] = Field(default_factory=dict, description="Schema-valid publish_log entry")
    error: Optional[str] = Field(default=None, description="Error message if upload failed")


class CheckpointData(BaseModel):
    version: str
    project_id: str
    pipeline_type: str
    stage: str
    status: str
    timestamp: Optional[str] = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    human_approval_required: bool = False
    human_approved: bool = False
    error: Optional[str] = None


class PipelineStatus(BaseModel):
    project_id: str
    pipeline_type: Optional[str]
    completed_stages: list[str]
    next_stage: Optional[str]
    latest_checkpoint: Optional[CheckpointData] = None


class PipelineInfo(BaseModel):
    name: str
    version: str = "unknown"
    category: str = "unknown"
    stages: list[str] = Field(default_factory=list)
    description: str = ""


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_tools(
    capability: Optional[str] = None,
    status: Optional[str] = None,
    provider: Optional[str] = None,
    tier: Optional[str] = None,
) -> list[ToolSummary]:
    """List all registered OpenMontage tools with their status.

    Optionally filter by capability (e.g. 'tts', 'image_generation', 'video_post'),
    status ('available', 'unavailable'), provider (e.g. 'elevenlabs', 'ffmpeg'), or
    tier ('core', 'voice', 'enhance', 'generate', 'source', 'analyze', 'publish').
    """
    tools = list(registry._tools.values())

    if capability:
        tools = [t for t in tools if t.capability == capability]
    if provider:
        tools = [t for t in tools if t.provider == provider]
    if tier:
        tools = [t for t in tools if t.tier.value == tier]

    results = []
    for t in tools:
        tool_status = t.get_status().value
        if status and tool_status != status:
            continue
        results.append(ToolSummary(
            name=t.name,
            capability=t.capability,
            provider=t.provider,
            status=tool_status,
            tier=t.tier.value,
            runtime=t.runtime.value,
            stability=t.stability.value,
        ))
    return results


@mcp.tool()
def get_tool_info(tool_name: str) -> dict[str, Any]:
    """Get the full tool contract: input/output schemas, dependencies, cost info, etc.

    Use this before calling execute_tool to understand what inputs a tool accepts
    and what it returns. The input_schema field is a JSON Schema object.
    """
    tool = registry.get(tool_name)
    if tool is None:
        return {"error": f"Tool '{tool_name}' not found. Use list_tools to see available tools."}
    return tool.get_info()


@mcp.tool()
def get_capabilities() -> dict[str, list[dict[str, Any]]]:
    """Get all tools grouped by capability (tts, image_generation, video_post, etc.).

    Returns a dict mapping capability names to lists of tool info dicts.
    Useful for understanding what the system can do at a glance.
    """
    return registry.capability_catalog()


@mcp.tool()
def get_provider_menu() -> dict[str, Any]:
    """Get a human-ready provider menu with configured/unconfigured counts.

    Returns the preflight-style capability menu showing which providers are
    configured (have API keys) vs available but not yet set up.
    Also includes composition runtime availability (remotion, hyperframes, ffmpeg).
    """
    return registry.provider_menu_summary()


# ---------------------------------------------------------------------------
# Execution tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def execute_tool(
    tool_name: str,
    inputs: dict[str, Any],
) -> ExecuteResult:
    """Execute an OpenMontage tool by name with the given inputs.

    IMPORTANT: Use get_tool_info first to understand the tool's input_schema.
    The inputs dict must match the tool's expected parameters.

    Returns structured result with success status, output data, file artifacts,
    cost in USD, and execution duration.
    """
    import asyncio
    import logging as _logging

    _log = _logging.getLogger("mcp_server")
    _log.info("execute_tool called: %s inputs=%s", tool_name, json.dumps(_safe_inputs(inputs), ensure_ascii=False))

    tool = registry.get(tool_name)
    if tool is None:
        return ExecuteResult(
            success=False,
            error=f"Tool '{tool_name}' not found. Use list_tools to discover tools.",
        )

    tool_status = tool.get_status()
    if tool_status == ToolStatus.UNAVAILABLE:
        return ExecuteResult(
            success=False,
            error=f"Tool '{tool_name}' is unavailable — dependencies not met. "
                  f"Check get_tool_info for required dependencies.",
        )

    try:
        result = await asyncio.to_thread(tool.execute, inputs)
        _log.info("execute_tool done: %s success=%s duration=%.2fs",
                  tool_name, result.success, result.duration_seconds or 0)
        _log.info("execute_tool response: tool=%s success=%s data_keys=%s error=%s",
                  tool_name, result.success, list(result.data.keys()) if result.data else None,
                  result.error[:80] if result.error else None)
        return ExecuteResult(
            success=result.success,
            data=result.data,
            artifacts=result.artifacts,
            error=result.error,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            seed=result.seed,
            model=result.model,
        )
    except Exception as e:
        _log.exception("execute_tool exception: %s", tool_name)
        return ExecuteResult(
            success=False,
            error=f"Execution failed: {type(e).__name__}: {e}",
        )


@mcp.tool()
def upload_asset(
    project_id: str,
    filename: str,
    content_base64: str,
    mime_type: Optional[str] = None,
    sha256: Optional[str] = None,
    overwrite: bool = False,
) -> UploadAssetResult:
    """Upload client-local media into a project-scoped assets directory.

    The returned path is safe to pass to ``video_compose`` or an AI video
    provider on this server.  ``content_base64`` may be raw base64 or a
    ``data:...;base64,...`` URI.  Client-local paths are intentionally not
    accepted because the MCP server cannot access the caller's filesystem.
    """
    tool = registry.get("upload_asset")
    if tool is None:
        return UploadAssetResult(success=False, error="upload_asset tool is not registered")
    result = tool.execute({
        "project_id": project_id,
        "filename": filename,
        "content_base64": content_base64,
        "mime_type": mime_type,
        "sha256": sha256,
        "overwrite": overwrite,
        "mcp_session_id": get_mcp_session_id(),
    })
    data = result.data or {}
    batch = data.get("batch") or {}
    count = len(batch.get("assets", []))
    if result.success and batch:
        duration_ms = round((result.duration_seconds or 0) * 1000)
        request_id = get_mcp_request_id() or uuid.uuid4().hex
        batch_project = batch.get("project_id") or project_id
        _event("asset_uploaded", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
        _event("batch_collecting", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
    return UploadAssetResult(
        success=result.success,
        asset=data.get("asset", {}),
        asset_manifest=data.get("asset_manifest", {}),
        deduplicated=bool(data.get("deduplicated", False)),
        status=batch.get("status"),
        asset_count=count,
        message=f"已收到 {count} 张图片。你可以继续上传，上传完成后发送“生成视频”。" if batch else None,
        next_action="continue_upload_or_generate" if batch else None,
        batch_id=batch.get("batch_id"),
        error=result.error,
    )


@mcp.tool()
def upload_asset_chunk(
    operation: str,
    project_id: Optional[str] = None,
    filename: Optional[str] = None,
    total_bytes: Optional[int] = None,
    mime_type: Optional[str] = None,
    sha256: Optional[str] = None,
    upload_id: Optional[str] = None,
    offset: Optional[int] = None,
    chunk_base64: Optional[str] = None,
) -> dict[str, Any]:
    """Resumable upload for 1080p-class media through small MCP requests.

    Call in order: start, append one or more chunks, complete. Each chunk
    should be at most 1 MiB; the server verifies size, offset and SHA-256.
    """
    tool = registry.get("upload_asset_chunk")
    if tool is None:
        return {"success": False, "error": "upload_asset_chunk is not registered"}
    result = tool.execute({
        "operation": operation,
        "project_id": project_id,
        "filename": filename,
        "total_bytes": total_bytes,
        "mime_type": mime_type,
        "sha256": sha256,
        "upload_id": upload_id,
        "offset": offset,
        "chunk_base64": chunk_base64,
        "mcp_session_id": get_mcp_session_id(),
    })
    data = result.data or {}
    batch = data.get("batch") or {}
    count = len(batch.get("assets", []))
    if result.success and batch:
        duration_ms = round((result.duration_seconds or 0) * 1000)
        request_id = get_mcp_request_id() or uuid.uuid4().hex
        batch_project = batch.get("project_id") or project_id
        _event("asset_uploaded", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
        _event("batch_collecting", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
    public_data = {key: value for key, value in data.items() if key != "batch"}
    response = {"success": result.success, **public_data, "artifacts": result.artifacts, "error": result.error}
    if batch:
        response.update({"status": batch.get("status"), "asset_count": count, "message": f"已收到 {count} 张图片。你可以继续上传，上传完成后发送“生成视频”。", "next_action": "continue_upload_or_generate", "batch_id": batch.get("batch_id")})
    return response


@mcp.tool()
def create_remotion_video_share(
    project_id: Optional[str] = None,
    duration_per_image: float = 3.0,
    aspect_ratio: str = "9:16",
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Generate and share a Remotion photo video from images in this MCP session.

    Images and the session are intentionally implicit: upload them first with
    upload_asset or upload_asset_chunk, then call this tool with no paths.
    Call this when the user says "生成视频", "开始生成", or "就这些，生成吧".
    """
    started = time.monotonic()
    sid = get_mcp_session_id()
    request_id = get_mcp_request_id() or uuid.uuid4().hex
    try:
        digest, state = begin_render(sid, project_id)
    except Exception as exc:
        _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=session_hash(sid), status="failed", stage="session", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
        return {"success": False, "status": "failed", "stage": "session", "message": str(exc), "error": str(exc)}

    project = state["project_id"]
    batch_id = state["batch_id"]
    job_id = state["render_job_id"]
    assets = state.get("assets", [])
    try:
        duration = float(duration_per_image)
        if duration < 1 or duration > 30:
            raise ValueError("duration_per_image must be between 1 and 30 seconds")
        max_images = max(1, int(os.environ.get("OPENMONTAGE_MAX_SESSION_IMAGES", "20")))
        if len(assets) > max_images:
            raise ValueError(f"This workflow accepts at most {max_images} images per batch")
        if duration * len(assets) > 600:
            raise ValueError("The requested photo video exceeds the 600 second limit")
        dimensions = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
        if aspect_ratio not in dimensions:
            raise ValueError("aspect_ratio must be one of 9:16, 16:9, or 1:1")
        width, height = dimensions[aspect_ratio]
        root = _PROJECT_ROOT / "projects" / project
        safe_assets = []
        for asset in assets:
            path = Path(asset.get("path", "")).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError("session asset path is outside the project workspace") from exc
            if not path.is_file() or asset.get("type") != "image":
                raise ValueError(f"session asset is not a readable image: {path.name}")
            safe_assets.append({**asset, "source_tool": "upload_asset", "scene_id": f"photo-{len(safe_assets):04d}"})

        motion = ["zoom-in", "pan-left", "ken-burns", "pan-right"]
        cuts = []
        scene_plan = []
        for index, asset in enumerate(safe_assets):
            start_seconds = index * duration
            end_seconds = (index + 1) * duration
            animation = motion[index % len(motion)]
            cuts.append({
                "id": f"cut-{index:04d}", "source": asset["id"], "in_seconds": start_seconds,
                "out_seconds": end_seconds, "layer": "primary", "transition_in": "fade" if index else "cut",
                "transition_duration": 0.25 if index else 0, "transform": {"animation": animation},
            })
            scene_plan.append({
                "type": "image",
                "description": f"Uploaded customer photo {index + 1}",
                "shot_intent": "Present the uploaded photo with restrained camera motion",
                "narrative_role": "customer_photo",
                "hero_moment": index == 0,
                "shot_language": {"camera_movement": animation, "shot_size": "full-frame"},
            })
        edit_decisions = {
            "version": "1.0", "cuts": cuts, "render_runtime": "remotion",
            "renderer_family": "animation-first", "composition_mode": "templated",
            "metadata": {"title": title or f"{project} photo video", "compose_target": {"width": width, "height": height, "fit": "cover"}},
        }
        asset_manifest = {"version": "1.0", "assets": safe_assets, "metadata": {"project_id": project, "batch_id": batch_id}}
        output = root / "renders" / f"{batch_id}-{job_id}.mp4"
        profile = {"9:16": "tiktok", "16:9": "generic_hd", "1:1": "instagram_feed"}[aspect_ratio]
        _event("render_requested", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=len(safe_assets), render_job_id=job_id, status="rendering")
        _event("render_started", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=len(safe_assets), render_job_id=job_id, status="rendering")
        render_tool = registry.get("video_compose")
        if render_tool is None:
            raise RuntimeError("video_compose tool is not registered")
        render_result = render_tool.execute({"operation": "render", "edit_decisions": edit_decisions, "asset_manifest": asset_manifest, "scene_plan": scene_plan, "profile": profile, "output_path": str(output), "remotion_timeout_ms": 600000})
        if not render_result.success:
            raise RuntimeError(render_result.error or "Remotion render failed")
        video_path = str(output)
        update_session(sid, status="rendered", video_path=video_path)
        _event("render_completed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=len(safe_assets), render_job_id=job_id, status="rendered", duration_ms=round((time.monotonic() - started) * 1000))
    except Exception as exc:
        update_session(sid, status="failed", video_path=locals().get("video_path"), failure_stage="render")
        _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=len(assets), render_job_id=job_id, status="failed", stage="render", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
        return {"success": False, "status": "failed", "stage": "render", "batch_id": batch_id, "message": "视频渲染失败。", "error": str(exc)}

    upload_tool = registry.get("weiyun_upload")
    if upload_tool is None:
        error = "weiyun_upload tool is not registered"
        update_session(sid, status="failed", failure_stage="weiyun_upload", video_path=video_path)
        _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_upload", duration_ms=round((time.monotonic() - started) * 1000), error=error)
        return {"success": False, "status": "failed", "stage": "weiyun_upload", "batch_id": batch_id, "video_path": video_path, "message": "视频已渲染，但微云上传失败。", "error": error}
    _event("weiyun_publish_started", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="uploading")
    uploaded = upload_tool.execute({"video_path": video_path, "target_dir": "", "overwrite": False})
    if not uploaded.success:
        error = uploaded.error or "Weiyun upload failed"
        update_session(sid, status="failed", failure_stage="weiyun_upload", video_path=video_path)
        _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_upload", duration_ms=round((time.monotonic() - started) * 1000), error=error)
        return {"success": False, "status": "failed", "stage": "weiyun_upload", "batch_id": batch_id, "video_path": video_path, "message": "视频已渲染，但微云上传失败。", "error": error}

    file_id = (uploaded.data or {}).get("file_id")
    share_tool = registry.get("weiyun.gen_share_link")
    if share_tool is None or not file_id:
        error = "weiyun.gen_share_link is unavailable or upload returned no file_id"
        update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path)
        _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
        return {"success": False, "status": "failed", "stage": "weiyun_share", "batch_id": batch_id, "video_path": video_path, "message": "视频已上传，但微云分享链接生成失败。", "error": error}
    shared = share_tool.execute({"file_list": [{"file_id": file_id}], "share_name": title or f"{project}-{batch_id}"})
    if not shared.success:
        error = shared.error or "Weiyun share link failed"
        update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path)
        _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
        return {"success": False, "status": "failed", "stage": "weiyun_share", "batch_id": batch_id, "video_path": video_path, "message": "视频已上传，但微云分享链接生成失败。", "error": error}
    share_url = (shared.data or {}).get("short_url") or (shared.data or {}).get("share_url")
    if not share_url:
        error = "Weiyun share tool returned no share URL"
        update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path)
        _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
        return {"success": False, "status": "failed", "stage": "weiyun_share", "batch_id": batch_id, "video_path": video_path, "message": "视频已上传，但微云分享链接生成失败。", "error": error}
    update_session(sid, status="published", share_url=share_url, video_path=video_path)
    _event("weiyun_publish_completed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, asset_count=len(safe_assets), status="published", duration_ms=round((time.monotonic() - started) * 1000))
    return {"success": True, "status": "published", "asset_count": len(safe_assets), "message": "视频已生成，点击下面的微云链接查看。", "share_url": share_url, "video_path": video_path, "duration_seconds": duration * len(safe_assets), "batch_id": batch_id}


@mcp.tool()
def s3_upload(
    video_path: str,
    visibility: str = "public",
    project_id: Optional[str] = None,
    object_key: Optional[str] = None,
    expire_seconds: Optional[int] = None,
    make_download_page: bool = False,
    additional_files: Optional[list[str]] = None,
    page_title: Optional[str] = None,
    platform_label: str = "s3",
) -> S3UploadResult:
    """Upload a rendered video to an S3-compatible object store.

    Returns a public permanent link or a time-limited pre-signed GET URL,
    and optionally builds a standalone HTML download page for multi-file
    delivery.  Configuration is read from the server environment variables
    (S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, etc.); these
    must be set in the OpenMontage ``.env`` before calling.
    """
    tool = registry.get("s3_upload")
    if tool is None:
        return S3UploadResult(success=False, error="s3_upload tool is not registered")

    if visibility not in ("public", "private"):
        return S3UploadResult(success=False, error="visibility must be 'public' or 'private'")

    inputs: dict[str, Any] = {
        "video_path": video_path,
        "visibility": visibility,
        "project_id": project_id,
        "object_key": object_key,
        "make_download_page": make_download_page,
        "page_title": page_title,
        "platform_label": platform_label,
    }
    if expire_seconds is not None:
        inputs["expire_seconds"] = expire_seconds
    if additional_files is not None:
        inputs["additional_files"] = additional_files

    result = tool.execute(inputs)
    data = result.data or {}
    return S3UploadResult(
        success=result.success,
        url=data.get("url"),
        object_key=data.get("object_key"),
        bucket=data.get("bucket"),
        visibility=data.get("visibility"),
        expires_at=data.get("expires_at"),
        download_page_url=data.get("download_page_url"),
        uploaded_files=data.get("uploaded_files") or [],
        publish_log=data.get("publish_log") or {},
        error=result.error,
    )


@mcp.tool()
def dry_run_tool(
    tool_name: str,
    inputs: dict[str, Any],
) -> DryRunResult:
    """Preflight check for a tool execution — shows estimated cost and runtime.

    Does NOT execute the tool. Use this before execute_tool for paid operations
    to understand what the call will cost and how long it will take.
    """
    tool = registry.get(tool_name)
    if tool is None:
        return DryRunResult(
            tool=tool_name,
            estimated_cost_usd=0.0,
            estimated_runtime_seconds=0.0,
            status="not_found",
            would_execute=False,
        )

    info = tool.dry_run(inputs)
    return DryRunResult(
        tool=info["tool"],
        estimated_cost_usd=info["estimated_cost_usd"],
        estimated_runtime_seconds=info["estimated_runtime_seconds"],
        status=info["status"],
        would_execute=info["would_execute"],
    )


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_pipelines() -> list[str]:
    """List all available pipeline manifest names.

    Each pipeline is a complete production workflow (e.g. 'animated-explainer',
    'cinematic', 'talking-head'). Use get_pipeline to load details.
    """
    return pipeline_loader.list_pipelines()


@mcp.tool()
def get_pipeline(name: str) -> dict[str, Any]:
    """Load and return a full pipeline manifest by name.

    The manifest defines stages, tools, review criteria, and approval gates.
    Pipeline names include: animated-explainer, animation, avatar-spokesperson,
    cinematic, clip-factory, documentary-montage, hybrid, localization-dub,
    podcast-repurpose, screen-demo, talking-head, framework-smoke.
    """
    try:
        manifest = pipeline_loader.load_pipeline(name)
        return manifest
    except FileNotFoundError as e:
        return {"error": str(e)}


@mcp.tool()
def get_pipeline_stages(pipeline_name: str) -> list[str]:
    """Get the ordered list of stages for a specific pipeline.

    Returns stage names in execution order (e.g. research -> proposal -> script
    -> scene_plan -> assets -> edit -> compose -> publish).
    """
    try:
        manifest = pipeline_loader.load_pipeline(pipeline_name)
        return pipeline_loader.get_stage_order(manifest)
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Checkpoint tools
# ---------------------------------------------------------------------------


def _pipeline_dir() -> Path:
    return _PROJECT_ROOT / "pipeline"


@mcp.tool()
def read_checkpoint(
    project_id: str,
    stage: str,
) -> CheckpointData | dict[str, str]:
    """Read a checkpoint for a specific project and stage.

    Returns the checkpoint data including artifacts, status, and timestamps.
    If no checkpoint exists, returns an error message.
    """
    cp = ckpt.read_checkpoint(_pipeline_dir(), project_id, stage)
    if cp is None:
        return {"error": f"No checkpoint found for project '{project_id}' stage '{stage}'"}
    return CheckpointData(
        version=cp.get("version", "1.0"),
        project_id=cp.get("project_id", project_id),
        pipeline_type=cp.get("pipeline_type", "unknown"),
        stage=cp.get("stage", stage),
        status=cp.get("status", "unknown"),
        timestamp=cp.get("timestamp"),
        artifacts=cp.get("artifacts", {}),
        human_approval_required=cp.get("human_approval_required", False),
        human_approved=cp.get("human_approved", False),
        error=cp.get("error"),
    )


@mcp.tool()
def get_latest_checkpoint(project_id: str) -> CheckpointData | dict[str, str]:
    """Get the most recent checkpoint for a project.

    Useful for checking pipeline progress — returns the last completed
    or in-progress stage with its artifacts.
    """
    cp = ckpt.get_latest_checkpoint(_pipeline_dir(), project_id)
    if cp is None:
        return {"error": f"No checkpoints found for project '{project_id}'"}
    return CheckpointData(
        version=cp.get("version", "1.0"),
        project_id=cp.get("project_id", project_id),
        pipeline_type=cp.get("pipeline_type", "unknown"),
        stage=cp.get("stage", "unknown"),
        status=cp.get("status", "unknown"),
        timestamp=cp.get("timestamp"),
        artifacts=cp.get("artifacts", {}),
        human_approval_required=cp.get("human_approval_required", False),
        human_approved=cp.get("human_approved", False),
        error=cp.get("error"),
    )


@mcp.tool()
def get_pipeline_status(
    project_id: str,
    pipeline_type: Optional[str] = None,
) -> PipelineStatus:
    """Get pipeline progress: completed stages and the next stage to run.

    Returns a structured status showing which stages are done, what's next,
    and the latest checkpoint data. Use this to decide what to execute next.
    """
    completed = ckpt.get_completed_stages(_pipeline_dir(), project_id, pipeline_type)
    next_stage = ckpt.get_next_stage(_pipeline_dir(), project_id, pipeline_type)
    latest = ckpt.get_latest_checkpoint(_pipeline_dir(), project_id)

    latest_data = None
    if latest:
        latest_data = CheckpointData(
            version=latest.get("version", "1.0"),
            project_id=latest.get("project_id", project_id),
            pipeline_type=latest.get("pipeline_type", "unknown"),
            stage=latest.get("stage", "unknown"),
            status=latest.get("status", "unknown"),
            timestamp=latest.get("timestamp"),
            artifacts=latest.get("artifacts", {}),
            human_approval_required=latest.get("human_approval_required", False),
            human_approved=latest.get("human_approved", False),
            error=latest.get("error"),
        )

    return PipelineStatus(
        project_id=project_id,
        pipeline_type=pipeline_type,
        completed_stages=completed,
        next_stage=next_stage,
        latest_checkpoint=latest_data,
    )


@mcp.tool()
def write_checkpoint(
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict[str, Any],
    pipeline_type: Optional[str] = None,
    style_playbook: Optional[str] = None,
    checkpoint_policy: str = "guided",
    human_approval_required: bool = False,
    human_approved: bool = False,
    review: Optional[dict[str, Any]] = None,
    cost_snapshot: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Write a checkpoint to persist pipeline state.

    Called after each stage completes to save artifacts and state.
    The project resumes from the last checkpoint if interrupted.
    """
    try:
        path = ckpt.write_checkpoint(
            _pipeline_dir(),
            project_id,
            stage,
            status,
            artifacts,
            pipeline_type=pipeline_type,
            style_playbook=style_playbook,
            checkpoint_policy=checkpoint_policy,
            human_approval_required=human_approval_required,
            human_approved=human_approved,
            review=review,
            cost_snapshot=cost_snapshot,
            error=error,
            metadata=metadata,
        )
        return {"success": True, "path": str(path)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Publish-tier tools (rsync, export bundle)
# ---------------------------------------------------------------------------


@mcp.tool()
def rsync_upload_artifact(
    source_path: str,
    remote_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a rendered artifact to a public server via SSH/rsync.

    Configuration is read from .env (RSYNC_* variables).
    Returns remote_path and download_url when RSYNC_PUBLIC_BASE_URL is set.
    """
    tool = registry.get("rsync_upload_artifact")
    if tool is None:
        return {"success": False, "error": "rsync_upload_artifact tool is not registered"}
    result = tool.execute({
        "source_path": source_path,
        "remote_name": remote_name,
        "dry_run": dry_run,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
def export_bundle(
    video_path: str,
    project_name: Optional[str] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Package a rendered video with metadata into a self-contained export bundle.

    Writes a schema-valid publish_log with status='exported'.
    """
    tool = registry.get("export_bundle")
    if tool is None:
        return {"success": False, "error": "export_bundle tool is not registered"}
    result = tool.execute({
        "video_path": video_path,
        "project_name": project_name,
        "chapters": chapters,
        "metadata": metadata,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
def weiyun_upload(
    video_path: str,
    target_dir: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload a rendered video to Tencent Weiyun (腾讯微云) via the MCP-token flow.

    Token-based upload (no QR-code login / cookies needed). Reads
    WEIYUN_MCP_TOKEN from the server environment (.env). Returns the Weiyun
    file_id and filename on success. Configure the token in the OpenMontage
    `.env` before calling. This is the token-based counterpart to the
    cookie-based weiyun_publish tool.
    """
    tool = registry.get("weiyun_upload")
    if tool is None:
        return {"success": False, "error": "weiyun_upload tool is not registered"}
    result = tool.execute({
        "video_path": video_path,
        "target_dir": target_dir,
        "overwrite": overwrite,
        "mcp_session_id": get_mcp_session_id(),
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
def weiyun_gen_share_link(
    file_list: list[str] = [],
    dir_list: list[str] = [],
    share_name: str = "",
    passwd: str = "",
) -> dict[str, Any]:
    """Generate a shareable link for files in Tencent Weiyun (腾讯微云).

    Accepts a list of file paths or directories and returns a short URL
    that can be shared. Configure WEIYUN_MCP_TOKEN in .env before calling.
    """
    tool = registry.get("weiyun.gen_share_link")
    if tool is None:
        return {"success": False, "error": "weiyun.gen_share_link tool is not registered"}
    inputs: dict[str, Any] = {"mcp_session_id": get_mcp_session_id()}
    if file_list:
        inputs["file_list"] = file_list
    if dir_list:
        inputs["dir_list"] = dir_list
    if share_name:
        inputs["share_name"] = share_name
    if passwd:
        inputs["passwd"] = passwd
    result = tool.execute(inputs)
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


# ---------------------------------------------------------------------------
# Bearer token auth
# ---------------------------------------------------------------------------


def _load_mcp_token() -> Optional[str]:
    """Load the MCP API token from the MCP_API_TOKEN env var (or .env file).

    registry._load_dotenv() already loads .env into os.environ, so this usually
    resolves on the first try. Reading the .env file directly is a fallback for
    cases where dotenv was not applied.
    """
    token = os.environ.get("MCP_API_TOKEN")
    if token:
        return token.strip()
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("MCP_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


class BearerTokenAuthMiddleware:
    """ASGI middleware enforcing `Authorization: Bearer <token>` on every HTTP request.

    Returns 401 with a WWW-Authenticate challenge when the header is missing or
    wrong. Comparison uses hmac.compare_digest to avoid timing attacks.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Extract client info for logging
        client_host = ""
        client_port = ""
        if scope.get("client"):
            client_host = scope["client"][0]
            client_port = scope["client"][1]

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Log request with method and path
        auth_present = bool(headers.get(b"authorization", b"").startswith(b"Bearer "))
        request_session = headers.get(b"mcp-session-id", b"").decode("ascii", errors="ignore").strip() or None
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore").strip() or uuid.uuid4().hex
        _log.info("Request: %s %s from %s:%s auth=%s session_hash=%s request_id=%s", method, path, client_host, client_port, "YES" if auth_present else "NO", session_hash(request_session), request_id)

        # Read and log request body for POST to /mcp
        if method == "POST" and path == "/mcp":
            body = b""
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            _log.info("Body received from %s:%s bytes=%d session_hash=%s request_id=%s", client_host, client_port, len(body), session_hash(request_session), request_id)
            # Re-dispatch body by wrapping receive
            async def _receive():
                return {"type": "http.request", "body": body, "more_body": False}
            scope["_body_consumed"] = True
        else:
            _receive = receive

        provided = headers.get(b"authorization", b"")
        if not provided.startswith(b"Bearer "):
            _log.warning("401 Unauthorized: Missing Bearer token from %s:%s", client_host, client_port)
            return await self._reject(scope, _receive, send)

        token = provided[len(b"Bearer "):].strip()
        if not hmac.compare_digest(token, self._expected):
            _log.warning("401 Unauthorized: Invalid token from %s:%s", client_host, client_port)
            return await self._reject(scope, _receive, send)

        _log.info("Auth OK: %s:%s", client_host, client_port)
        session_id = request_session
        session_token = set_mcp_session_id(session_id)
        request_token = set_mcp_request_id(request_id)
        try:
            return await self.app(scope, _receive, send)
        finally:
            reset_mcp_request_id(request_token)
            reset_mcp_session_id(session_token)

    @staticmethod
    async def _reject(scope, receive, send):
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "unauthorized", "message": "Missing or invalid Bearer token."},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="openmontage-mcp"'},
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gen-token":
        print(secrets.token_urlsafe(32))
        sys.exit(0)

    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    _log.info("Starting OpenMontage MCP server on port 8900 (transport=%s)", transport)
    _log.info("%s/%s tools available", _AVAILABLE, len(_discovered))

    _api_token = _load_mcp_token()
    if _api_token:
        _log.info("Bearer token auth ENABLED — clients must send 'Authorization: Bearer <MCP_API_TOKEN>'")
    else:
        _log.warning("MCP_API_TOKEN is not set — server is running WITHOUT authentication.")
        _log.warning("Do NOT expose port 8900 to the public internet until you set a token.")
        _log.warning("Generate one with:  python mcp_server.py gen-token")

    if transport == "streamable-http":
        import socket
        import uvicorn
        # Dual-stack socket: IPv6 + IPv4 via IPV6_V6ONLY=0
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", 8900))
        sock.listen(2048)
        app = mcp.streamable_http_app()
        if _api_token:
            app = BearerTokenAuthMiddleware(app, _api_token)
        config = uvicorn.Config(app, fd=sock.fileno())
        server = uvicorn.Server(config)
        import asyncio
        asyncio.run(server.serve())
    else:
        mcp.run(transport=transport)
