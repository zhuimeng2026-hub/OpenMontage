"""OpenMontage MCP Server — exposes tools, pipelines, and checkpoints over MCP.

Run with streamable-http (for remote access):
    python mcp_server.py

Run with stdio (for local agent integration):
    python mcp_server.py stdio
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Optional

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
    stateless_http=True,
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
    _log.info("execute_tool called: %s", tool_name)

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
    })
    data = result.data or {}
    return UploadAssetResult(
        success=result.success,
        asset=data.get("asset", {}),
        asset_manifest=data.get("asset_manifest", {}),
        deduplicated=bool(data.get("deduplicated", False)),
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
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        provided = headers.get(b"authorization", b"")
        if not provided.startswith(b"Bearer "):
            return await self._reject(scope, receive, send)
        token = provided[len(b"Bearer "):].strip()
        if not hmac.compare_digest(token, self._expected):
            return await self._reject(scope, receive, send)
        return await self.app(scope, receive, send)

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
    print(f"[mcp_server] Starting OpenMontage MCP server on port 8900 (transport={transport})")
    print(f"[mcp_server] {_AVAILABLE}/{len(_discovered)} tools available")

    _api_token = _load_mcp_token()
    if _api_token:
        print("[mcp_server] Bearer token auth ENABLED — clients must send 'Authorization: Bearer <MCP_API_TOKEN>'")
    else:
        print("[mcp_server] WARNING: MCP_API_TOKEN is not set — server is running WITHOUT authentication.")
        print("[mcp_server] Do NOT expose port 8900 to the public internet until you set a token.")
        print("[mcp_server] Generate one with:  python mcp_server.py gen-token")

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
