"""claude-video → OpenMontage recompose adapter.

Receives claude-video's RunResult over MCP (from the ``recompose`` tool
described in ``/opt/claude-video/docs/MCP_SERVER_PRD.md`` §2.6) and submits
it as an OpenMontage project under
``projects/users/<user_openid>/<project_id>/``.

Spec: ``docs/claude-video-integration.md``
Whitelist audit: ``docs/claude-video-whitelist-audit.md``
Cross-repo contract docs (claude-video side, 2026-08-23):

- ``docs/BFF_API_CONTRACT.md``        — Phase 2.7 BFF HTTP/SSE surface
- ``docs/OAUTH_TRUST_MODEL.md``       — cookie + user_openid integrity model
- ``docs/OPENMONTAGE_NAME_MAP.md``    — pipeline/style name table
- ``docs/MCP_SERVER_PRD.md §2.6``    — `recompose` tool signature + return
- ``docs/MCP_SERVER_PRD.md §2.6.3``  — 6-code error envelope (PIPED THROUGH THIS FILE)
- ``tests/fixtures/sample_runresult.json``        — canonical RunResult shape
- ``tests/fixtures/error_envelope_*.json``        — error envelope fixtures

This adapter receives a ``recompose`` call that claude-video's MCP server
already authorized (cookie + ``video_id`` ↔ ``user_openid`` check happened
upstream). OpenMontage does NOT re-verify the user — see
``docs/OAUTH_TRUST_MODEL.md`` for the trust model.

Error envelope (must stay 1:1 with claude-video's 6-code table,
``docs/MCP_SERVER_PRD.md §2.6.3``):

    pipeline_not_in_whitelist   pipeline not in GPU-free whitelist
    video_id_unknown            source.frames_dir / masks_dir / etc. missing on disk
    user_not_found              user_openid was empty when required
    assets_copy_failed          mkdir / copy under projects/users/.../assets/ failed
    pipeline_stage_failed       OM's pipeline orchestrator reported failure (TO DO: not yet raised here)
    gpu_required                pipeline would need a GPU provider (defense-in-depth on this side)

Layers:
  Layer 1 (this file) — BaseTool subclass + real execute() body.
  Layer 2 (done)      — init_project + asset copy + source_meta.json + initial
                        checkpoint hand-off.
  Layer 2 (TO DO)     — programmatic stage-director orchestration (this
                        codebase's pipelines are agent-driven; an LLM in the
                        loop walks the stage directors. The adapter prepares
                        inputs; the orchestrator runs the stages.)
  Layer 3 (omitted)   — .agents/skills/claude-video-integration/SKILL.md.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
)
from lib.checkpoint import init_project, write_checkpoint
from lib import paths as _lib_paths


# ---------------------------------------------------------------------------
# Constants — exposed at module level so tests + registry can import.
# ---------------------------------------------------------------------------

# GPU-free pipeline whitelist. Verified against registry.gpu_required_tools()
# in docs/claude-video-whitelist-audit.md (2026-08-23).
#
# Spelling note: ``podcast-repurpose`` is the OpenMontage pipeline file
# name (pipeline_defs/podcast-repurpose.yaml). claude-video's todo.md
# §2.6.2 originally had a typo (commit 644d7b0 fixed it on their side).
ALLOWED_PIPELINES: frozenset[str] = frozenset({
    "clip-factory",
    "documentary-montage",
    "podcast-repurpose",
    "localization-dub",
    "hybrid",
    "screen-demo",
})

# Optional tools that are GPU-required and must NOT run on a CPU-only host.
# Adapter enforces these at the entry boundary regardless of the pipeline's
# own optional_tools list. Pass extra={"allow_gpu_optional_tools": True} to
# opt in on a GPU host.
HARD_DISABLED_OPTIONAL_TOOLS: frozenset[str] = frozenset({
    "lip_sync",
})

# Error codes — must stay 1:1 with /opt/claude-video/docs/MCP_SERVER_PRD.md
# §2.6.3 and /opt/claude-video/tests/fixtures/error_envelope_*.json.
ERROR_PIPELINE_NOT_IN_WHITELIST = "pipeline_not_in_whitelist"
ERROR_VIDEO_ID_UNKNOWN = "video_id_unknown"
ERROR_USER_NOT_FOUND = "user_not_found"
ERROR_ASSETS_COPY_FAILED = "assets_copy_failed"
ERROR_PIPELINE_STAGE_FAILED = "pipeline_stage_failed"
ERROR_GPU_REQUIRED = "gpu_required"

# Project layout. Mirrors docs/web-multiuser-auth.md.
USER_ROOT_TEMPLATE = "projects/users/{user_openid}"

# Where the MCP server is reachable on the host; the Backlot board sits on
# the same port. Used for the `backlot_url` field in ToolResult.data.
DEFAULT_BACKLOT_BASE_URL = os.environ.get(
    "OM_BACKLOT_BASE_URL", "http://localhost:8900"
)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ClaudeVideoComposeTool(BaseTool):
    """Adapter that consumes claude-video's ``recompose`` output and submits
    an OpenMontage project.

    Trust model
    -----------
    OpenMontage treats ``inputs["user_openid"]`` as **untrusted** — it
    lands projects under ``projects/users/<user_openid>/`` based purely
    on this string. The horizontal-isolation guarantee depends entirely
    on claude-video's Phase 2.8 BFF: only that component verifies the
    ``WATCH_SESSION`` cookie and only then passes the corresponding
    ``user_openid`` through. See
    ``/opt/claude-video/docs/OAUTH_TRUST_MODEL.md`` §"What OpenMontage
    verifies" for the contract this adapter assumes.

    Pipeline execution model
    ------------------------
    This adapter does NOT run OpenMontage pipelines programmatically —
    the pipelines in this repo are agent-driven (stage directors are
    skills the agent reads). The adapter's job is to land the source
    artifacts + metadata + initial ``checkpoint_idea.json`` so the
    orchestrator can pick it up next. The Backlot board surfaces
    ``status="in_progress"`` checkpoints as needing a stage director to
    take over.
    """

    name = "claude_video.compose"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "external_recompose"
    provider = "claude_video"
    stability = ToolStability.BETA

    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    # Calls run inside the OpenMontage process (no external network beyond
    # what a normal pipeline run already does). LOCAL is honest.
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = (
        "claude-video integration requires OpenMontage's main MCP server on "
        ":8900. See docs/claude-video-integration.md and "
        "/opt/claude-video/docs/BFF_API_CONTRACT.md (BFF side)."
    )

    # Layer 3 pointer. Today there is no .agents/skills/claude-video-integration/
    # SKILL.md — when it lands, replace this with ["claude-video-integration"].
    agent_skills: list[str] = []

    capabilities: list[str] = ["external_recompose", "video_compose_external"]
    best_for = [
        "Forwarding a claude-video /watch RunResult to a GPU-free OpenMontage "
        "pipeline (clip-factory, documentary-montage, etc.).",
        "Producing remixes, dubs, montages, and social-clip cascades from "
        "watch artifacts without leaving the OpenMontage storyboard.",
    ]
    not_good_for = [
        "Pipelines that require local GPU (FLUX, Wan, Hunyuan, CogVideo, "
        "talking-head, lip_sync) — guard rejects at the adapter boundary.",
        "Calls without a valid user_openid — multi-tenant safety depends "
        "on claude-video's BFF; OpenMontage does not re-verify.",
    ]
    side_effects = [
        "creates projects/users/<user_openid>/<project_id>/{artifacts,assets,renders}/",
        "copies source.frames_dir / masks_dir / vtt_path / video_path into "
        "assets/{frames,masks,audio,video}/ subdirs",
        "writes artifacts/source_meta.json with transcript_segments + duration",
        "writes artifacts/source_media_review.json with the original RunResult",
        "writes checkpoint_idea.json with status=in_progress to hand off to a "
        "stage director (Backlot boards will surface this as needing pickup)",
    ]

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=1024,
        network_required=False,
    )
    retry_policy = RetryPolicy(max_retries=0, backoff_seconds=0.0)

    user_visible_verification = [
        "After ToolResult returns, projects/users/<user_openid>/<project_id>/ "
        "exists with assets/{frames,masks,audio,video}/ populated and "
        "artifacts/source_meta.json readable.",
        "A stage director (or human) reading skills/pipelines/<pipeline>/"
        "idea-director.md will find the project in an `in_progress` state with "
        "source_meta.json next to it.",
        "ToolResult.data['backlot_url'] is visitable in a browser on the host "
        "running the OpenMontage MCP server (default :8900).",
    ]

    # ---- JSON Schemas ----

    input_schema: dict[str, Any] = {
        "type": "object",
        "required": ["user_openid", "source", "pipeline"],
        "additionalProperties": False,
        "properties": {
            "user_openid": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "WeChat openid (or future unionid) of the requesting user. "
                    "Paths land under projects/users/<user_openid>/. OpenMontage "
                    "DOES NOT verify this string; integrity guarantee depends "
                    "on claude-video's Phase 2.8 BFF."
                ),
            },
            "project_id": {
                "type": "string",
                "description": (
                    "Override the project identifier; defaults to "
                    "source.video_id. Must be unique within the user dir."
                ),
            },
            "source": {
                "type": "object",
                "required": ["video_id", "frames_dir", "duration_seconds",
                             "transcript_segments"],
                "additionalProperties": False,
                "properties": {
                    "video_id": {
                        "type": "string",
                        "description": (
                            "Stable video identifier from claude-video's "
                            "session_store. 12 hex chars by default."
                        ),
                    },
                    "frames_dir": {
                        "type": "string",
                        "description": "Absolute path containing frame_NNNN.jpg files.",
                    },
                    "masks_dir": {
                        "type": ["string", "null"],
                        "description": (
                            "Absolute path containing mask_NNNN.png files, "
                            "or null if no segmentation was run."
                        ),
                    },
                    "vtt_path": {
                        "type": ["string", "null"],
                        "description": (
                            "Absolute path to a .vtt caption file if "
                            "captions or Whisper produced one."
                        ),
                    },
                    "video_path": {
                        "type": ["string", "null"],
                        "description": (
                            "Absolute path to the source mp4 if retained "
                            "after /watch; null otherwise."
                        ),
                    },
                    "duration_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Source video duration in seconds (for stage math).",
                    },
                    "transcript_segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["start", "end", "text"],
                            "properties": {
                                "start": {"type": "number", "minimum": 0},
                                "end": {"type": "number", "minimum": 0},
                                "text": {"type": "string"},
                            },
                        },
                        "description": (
                            "Structured transcript segments (from VTT or "
                            "Whisper). Each item: {start, end, text}."
                        ),
                    },
                },
            },
            "pipeline": {
                "type": "string",
                "enum": sorted(ALLOWED_PIPELINES),
                "description": (
                    "GPU-free OpenMontage pipeline to run. See "
                    "docs/claude-video-whitelist-audit.md. Must match an "
                    "existing pipeline_defs/<name>.yaml."
                ),
            },
            "style": {
                "type": "string",
                "default": "clean-professional",
                "description": (
                    "OM playbook name. Mapped against "
                    "/opt/claude-video/docs/OPENMONTAGE_NAME_MAP.md."
                ),
            },
            "extra": {
                "type": "object",
                "default": {},
                "additionalProperties": True,
                "description": (
                    "Passthrough. Reserved keys: ``allow_gpu_optional_tools`` "
                    "(bool, default False — must be True to enable "
                    "HARD_DISABLED_OPTIONAL_TOOLS); ``disabled_tools`` "
                    "(list[str], appended to the hard-disable set); "
                    "``backlot_base_url`` (str, override the Backlot board "
                    "URL for tests)."
                ),
            },
        },
    }

    output_schema: dict[str, Any] = {
        "type": "object",
        "required": ["status", "code", "project_id", "user_openid"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["submitted", "skipped"],
                "description": (
                    "``submitted`` once all side effects succeeded; "
                    "``skipped`` if a precondition rejected before any "
                    "filesystem work. Maps to MCP ToolError vs success."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Machine-readable result code. On success: ``ok``. On "
                    "failure: one of the 6 error codes from "
                    "ERROR_* constants."
                ),
            },
            "project_id": {"type": "string"},
            "user_openid": {"type": "string"},
            "pipeline": {"type": "string"},
            "style": {"type": "string"},
            "renders_path": {
                "type": "string",
                "description": (
                    "projects/users/<openid>/<id>/renders/ — the directory "
                    "where final.mp4 lands once the orchestrator completes "
                    "all stages."
                ),
            },
            "backlot_url": {
                "type": "string",
                "description": (
                    "URL for `python -m backlot open <project_id>`."
                ),
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Non-fatal notes (e.g., lip_sync was auto-disabled, "
                    "user added extra.disabled_tools)."
                ),
            },
            "copied_assets": {
                "type": "object",
                "description": (
                    "Post-copy absolute paths inside the project dir. "
                    "Keys: frames_dir, masks_dir, vtt_path, video_path. "
                    "Values are null when the corresponding input was "
                    "null OR the copy was skipped (e.g., masks_dir "
                    "absent on disk)."
                ),
                "additionalProperties": True,
            },
        },
    }

    artifact_schema: dict[str, Any] = {
        "writes": [
            "projects/users/<user_openid>/<project_id>/project.json",
            "projects/users/<user_openid>/<project_id>/artifacts/source_meta.json",
            "projects/users/<user_openid>/<project_id>/artifacts/source_media_review.json",
            "projects/users/<user_openid>/<project_id>/checkpoint_idea.json",
            "projects/users/<user_openid>/<project_id>/assets/frames/*",
            "projects/users/<user_openid>/<project_id>/assets/masks/*",
            "projects/users/<user_openid>/<project_id>/assets/video/*",
            "projects/users/<user_openid>/<project_id>/assets/audio/*",
            "projects/users/<user_openid>/<project_id>/renders/final.mp4  (by orchestrator, NOT this adapter)",
        ],
    }

    # ---- Main entry point ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """Validate, copy artifacts, write metadata + initial checkpoint.

        Step-by-step (kept short on purpose):

          1. Whitelist enforcement        → ``pipeline_not_in_whitelist``
          2. user_openid presence         → ``user_not_found``
          3. Source field validation      → ``assets_copy_failed`` (catch-all
                                            for malformed input that would
                                            fail later under copy)
          4. Path computation (no I/O)
          5. ``init_project``              → ``assets_copy_failed`` on error
          6. Source asset copy             → ``assets_copy_failed`` on error;
                                            ``video_id_unknown`` if source
                                            frames_dir is gone (GC'd by
                                            claude-video)
          7. source_meta.json + source_media_review.json write
          8. checkpoint_idea.json hand-off (status=in_progress)
          9. Return ToolResult with status='submitted'.
        """
        # 1. Whitelist
        pipeline = inputs.get("pipeline") if isinstance(inputs, dict) else None
        ok, err = check_pipeline_whitelist(pipeline or "")
        if not ok:
            return _err(ERROR_PIPELINE_NOT_IN_WHITELIST, err)

        # 2. user_openid presence — BFF overrides a missing cookie-binding
        #    with the empty string; we treat both empty and missing the same.
        user_openid = inputs.get("user_openid") if isinstance(inputs, dict) else None
        if not isinstance(user_openid, str) or not user_openid.strip():
            return _err(
                ERROR_USER_NOT_FOUND,
                "user_openid was required (BFF passed an empty value, or session "
                "is anonymous in a deploy that disallows it)."
            )

        # 3. Field validation
        ok2, err2 = _validate_inputs(inputs)
        if not ok2:
            return _err(ERROR_ASSETS_COPY_FAILED, err2)

        # 4. Path computation. Access PROJECTS_DIR via the module (not as
        #    a captured value) so test fixtures that monkeypatch
        #    ``lib.paths.PROJECTS_DIR`` to a tmp_path actually reach us.
        source = inputs["source"]
        project_id = inputs.get("project_id") or source["video_id"]
        pipeline_dir = _lib_paths.PROJECTS_DIR / "users" / user_openid
        project_dir = pipeline_dir / project_id

        # 5. init_project (idempotent — re-running merges).
        try:
            init_project(
                project_id,
                title=f"claude-video {source['video_id']}",
                pipeline_type=pipeline,
                pipeline_dir=pipeline_dir,
                style_playbook=inputs.get("style") or "clean-professional",
            )
        except Exception as exc:
            return _err(
                ERROR_ASSETS_COPY_FAILED,
                f"failed to initialize project dir at {project_dir}: "
                f"{type(exc).__name__}: {exc}"
            )

        # 6. Copy. If frames_dir doesn't exist, that's a video_id-level
        #    problem (the source has been GC'd) — surface as video_id_unknown.
        frames_src = Path(source["frames_dir"])
        if not frames_src.exists():
            return _err(
                ERROR_VIDEO_ID_UNKNOWN,
                f"video_id {source['video_id']!r} artifacts are gone — "
                f"frames_dir {frames_src} does not exist. claude-video's "
                f"session_store likely GC'd the work_dir. Re-run /watch."
            )
        copy_result = _copy_source_assets(source, project_dir)
        if not copy_result.ok:
            return _err(ERROR_ASSETS_COPY_FAILED, copy_result.error)

        # 7. source_meta.json + source_media_review.json
        extra = inputs.get("extra") or {}
        style = inputs.get("style") or "clean-professional"
        backlot_base = (
            extra.get("backlot_base_url") if isinstance(extra.get("backlot_base_url"), str)
            else DEFAULT_BACKLOT_BASE_URL
        )

        warnings: list[str] = []
        if pipeline == "localization-dub" and not extra.get("allow_gpu_optional_tools"):
            warnings.append(
                "localization-dub: lip_sync auto-disabled (GPU-required, no "
                "allow_gpu_optional_tools in extra). Pipeline will run "
                "subtitles + TTS dub only."
            )
        if extra.get("disabled_tools"):
            warnings.append(
                f"user-passed disabled_tools: {list(extra['disabled_tools'])}"
            )

        _write_source_meta(project_dir, source, copy_result.paths,
                            pipeline=pipeline, style=style,
                            extra=extra)
        _write_source_media_review(project_dir, source, copy_result.paths,
                                    pipeline=pipeline, style=style)

        # 8. Initial checkpoint hand-off (status=in_progress — orchestrator
        #    takes over via stage directors).
        try:
            write_checkpoint(
                pipeline_dir,
                project_id,
                "idea",
                "in_progress",
                artifacts={
                    # Anchor the artifact for the next stage director. The
                    # canonical `brief` artifact is emitted by the idea-stage
                    # director itself; we use `source_meta` as the
                    # non-canonical anchor so validate_checkpoint() doesn't
                    # reject the in_progress write.
                    "source_meta_ref": copy_result.paths.get("source_meta_path"),
                },
                pipeline_type=pipeline,
                style_playbook=style,
                checkpoint_policy="guided",
                human_approval_required=False,  # idea stage director will flip this when ready
                metadata={
                    "submitted_via": "claude_video.compose",
                    "video_id": source["video_id"],
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "warnings": warnings,
                },
            )
        except Exception as exc:
            return _err(
                ERROR_PIPELINE_STAGE_FAILED,
                f"failed to write initial checkpoint_idea.json: "
                f"{type(exc).__name__}: {exc}"
            )

        # 9. Success.
        return ToolResult(
            success=True,
            data={
                "status": "submitted",
                "code": "ok",
                "project_id": project_id,
                "user_openid": user_openid,
                "pipeline": pipeline,
                "style": style,
                "renders_path": str(project_dir / "renders"),
                "backlot_url": f"{backlot_base.rstrip('/')}/backlot/{project_id}",
                "warnings": warnings,
                "copied_assets": copy_result.paths,
                "next_actions": [
                    "Stage director picks up via skills/pipelines/"
                    f"{pipeline}/idea-director.md",
                    "Backlot surfaces status=in_progress until idea-director "
                    "reaches awaiting_human",
                ],
            },
            artifacts=[str(project_dir)],
        )


# ---------------------------------------------------------------------------
# Module helpers — pure functions, no I/O unless noted.
# ---------------------------------------------------------------------------


class _CopyResult:
    """Outcome of `_copy_source_assets`. Either ok=True with paths set, or
    ok=False with a precise error message destined for the
    ``assets_copy_failed`` ToolResult envelope."""

    def __init__(self, ok: bool, error: str = "", paths: Optional[dict[str, str | None]] = None) -> None:
        self.ok = ok
        self.error = error
        self.paths = paths or {}


def _copy_source_assets(
    source: dict[str, Any],
    project_dir: Path,
) -> _CopyResult:
    """Copy the four RunResult paths into the project assets/ tree.

    Layout (matches the conventions in `lib/checkpoint.init_project`):
        assets/frames/      ← source.frames_dir (recursive dir copy)
        assets/masks/       ← source.masks_dir  (recursive dir copy; null = skip)
        assets/video/       ← source.video_path (single file; null = skip)
        assets/audio/<name> ← source.vtt_path   (single file; null = skip)

    Returns ``_CopyResult(ok=True, paths={...})`` on success. On any
    filesystem failure returns ``_CopyResult(ok=False, error=<message>)``
    with paths partial — callers should treat the partial state as
    project-corruption and surface ``assets_copy_failed``.
    """
    paths: dict[str, str | None] = {}
    frames_dst = project_dir / "assets" / "frames"
    masks_dst = project_dir / "assets" / "masks"
    video_dst = project_dir / "assets" / "video"
    audio_dst = project_dir / "assets" / "audio"

    # 1. frames_dir → assets/frames/ (recursive copy)
    frames_src = Path(source["frames_dir"])
    if not frames_src.is_dir():
        return _CopyResult(
            ok=False,
            error=(
                f"frames_dir {frames_src} is not a directory; expected a "
                f"directory of frame_NNNN.jpg files."
            ),
        )
    try:
        frames_dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(frames_src, frames_dst, dirs_exist_ok=True)
    except (shutil.Error, OSError) as exc:
        return _CopyResult(ok=False, error=(
            f"failed to copy frames_dir {frames_src} -> {frames_dst}: "
            f"{type(exc).__name__}: {exc}"
        ))
    paths["frames_dir"] = str(frames_dst)

    # 2. masks_dir → assets/masks/ (skip if null)
    masks_src = source.get("masks_dir")
    if masks_src:
        masks_path = Path(masks_src)
        try:
            if masks_path.is_dir():
                masks_dst.mkdir(parents=True, exist_ok=True)
                shutil.copytree(masks_path, masks_dst, dirs_exist_ok=True)
                paths["masks_dir"] = str(masks_dst)
            elif masks_path.is_file():
                masks_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(masks_path, masks_dst / masks_path.name)
                paths["masks_dir"] = str(masks_dst / masks_path.name)
            else:
                paths["masks_dir"] = None  # path supplied but missing — caller still gets the rest
        except (shutil.Error, OSError) as exc:
            return _CopyResult(ok=False, error=(
                f"failed to copy masks_dir {masks_path} -> {masks_dst}: "
                f"{type(exc).__name__}: {exc}"
            ))
    else:
        paths["masks_dir"] = None

    # 3. video_path → assets/video/<basename>
    video_src = source.get("video_path")
    if video_src:
        video_path = Path(video_src)
        try:
            if not video_path.is_file():
                return _CopyResult(ok=False, error=(
                    f"video_path {video_path} is not a file."
                ))
            video_dst.mkdir(parents=True, exist_ok=True)
            target = video_dst / video_path.name
            shutil.copy2(video_path, target)
            paths["video_path"] = str(target)
        except (shutil.Error, OSError) as exc:
            return _CopyResult(ok=False, error=(
                f"failed to copy video_path {video_path} -> {video_dst}: "
                f"{type(exc).__name__}: {exc}"
            ))
    else:
        paths["video_path"] = None

    # 4. vtt_path → assets/audio/<basename>
    vtt_src = source.get("vtt_path")
    if vtt_src:
        vtt_path = Path(vtt_src)
        try:
            if not vtt_path.is_file():
                return _CopyResult(ok=False, error=(
                    f"vtt_path {vtt_path} is not a file."
                ))
            audio_dst.mkdir(parents=True, exist_ok=True)
            target = audio_dst / vtt_path.name
            shutil.copy2(vtt_path, target)
            paths["vtt_path"] = str(target)
        except (shutil.Error, OSError) as exc:
            return _CopyResult(ok=False, error=(
                f"failed to copy vtt_path {vtt_path} -> {audio_dst}: "
                f"{type(exc).__name__}: {exc}"
            ))
    else:
        paths["vtt_path"] = None

    return _CopyResult(ok=True, paths=paths)


def _write_source_meta(
    project_dir: Path,
    source: dict[str, Any],
    paths: dict[str, str | None],
    *,
    pipeline: str,
    style: str,
    extra: dict[str, Any],
) -> None:
    """Write artifacts/source_meta.json with the canonical RunResult
    snapshot + provenance metadata."""
    payload = {
        "video_id": source["video_id"],
        "frames_dir": paths.get("frames_dir"),
        "masks_dir": paths.get("masks_dir"),
        "vtt_path": paths.get("vtt_path"),
        "video_path": paths.get("video_path"),
        "duration_seconds": source["duration_seconds"],
        "transcript_segments": source["transcript_segments"],
        "claude_video_provenance": {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": pipeline,
            "style": style,
            "extra_keys": sorted(k for k in extra.keys() if isinstance(k, str)),
        },
    }
    paths["source_meta_path"] = str(
        project_dir / "artifacts" / "source_meta.json"
    )
    out = Path(paths["source_meta_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _write_source_media_review(
    project_dir: Path,
    source: dict[str, Any],
    paths: dict[str, str | None],
    *,
    pipeline: str,
    style: str,
) -> None:
    """Write artifacts/source_media_review.json — the supplementary
    artifact the checkpoint validator expects for projects that arrived
    from a `source_media_review`-carrying pipeline (per
    lib/checkpoint.SUPPLEMENTARY_ARTIFACTS). Stage directors read this to
    confirm the original source provenance before drafting the brief."""
    payload = {
        "video_id": source["video_id"],
        "duration_seconds": source["duration_seconds"],
        "frames_count": None,  # unknown without counting the dir; left None to avoid lying
        "transcript_segments_count": len(source["transcript_segments"]),
        "frames_dir": paths.get("frames_dir"),
        "video_path": paths.get("video_path"),
        "vtt_path": paths.get("vtt_path"),
        "masks_dir": paths.get("masks_dir"),
        "claude_video_origin": {
            "pipeline": pipeline,
            "style": style,
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    out = project_dir / "artifacts" / "source_media_review.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def project_dir_for(user_openid: str, project_id: str) -> Path:
    """Compute the project directory for a user/project pair.

    Mirrors docs/web-multiuser-auth.md. Pure function — no filesystem I/O.
    Returns a path RELATIVE to PROJECTS_DIR; callers needing an absolute
    path should resolve against ``lib.paths.PROJECTS_DIR`` themselves.
    """
    return Path(USER_ROOT_TEMPLATE.format(user_openid=user_openid)) / project_id


def check_pipeline_whitelist(pipeline: str) -> tuple[bool, str]:
    """Return (ok, error_message). Used by tests + by execute() to enforce
    the whitelist on every call. ``ok`` is False for any pipeline not in
    ALLOWED_PIPELINES, including typo variants like ``podcast-reproduce``."""
    if pipeline in ALLOWED_PIPELINES:
        return True, ""
    return False, (
        f"pipeline {pipeline!r} is not in the allowed whitelist. "
        f"Allowed: {sorted(ALLOWED_PIPELINES)}."
    )


def compute_disabled_tools(extra: dict[str, Any], *,
                          allow_gpu_optional_tools: bool = False) -> list[str]:
    """Compute the set of optional tools the adapter will block.

    Layers the policy in two pieces:
      1. ``HARD_DISABLED_OPTIONAL_TOOLS`` (e.g. ``lip_sync``) — added unless
         ``allow_gpu_optional_tools=True`` (the GPU-host opt-in).
      2. Any caller-passed ``extra["disabled_tools"]`` list[str] is appended.
    Returns a sorted list for stable logging.
    """
    base: set[str] = set()
    if not allow_gpu_optional_tools:
        base.update(HARD_DISABLED_OPTIONAL_TOOLS)
    user_extra = extra.get("disabled_tools") or []
    if isinstance(user_extra, list):
        for name in user_extra:
            if isinstance(name, str) and name:
                base.add(name)
    return sorted(base)


def _validate_inputs(inputs: dict[str, Any]) -> tuple[bool, str]:
    """Cheap runtime checks on top of JSON Schema input_schema.

    Rejects the obvious failure modes that would otherwise corrupt the
    project directory (None / "" / wrong types). Does not exhaustively
    validate every field — input_schema is the source of truth; this just
    gives precise error messages instead of KeyError / TypeError later.
    """
    if not isinstance(inputs, dict):
        return False, "inputs must be a dict"
    if not inputs.get("user_openid"):
        return False, "user_openid is required and non-empty"
    src = inputs.get("source")
    if not isinstance(src, dict):
        return False, "source must be a dict"
    for required in ("video_id", "frames_dir", "duration_seconds",
                     "transcript_segments"):
        if src.get(required) in (None, ""):
            return False, f"source.{required} is required"
    if not isinstance(src["transcript_segments"], list):
        return False, "source.transcript_segments must be a list"
    style = inputs.get("style")
    if style is not None and not isinstance(style, str):
        return False, "style must be a string when provided"
    return True, ""


def _err(code: str, message: str) -> ToolResult:
    """Build a typed-error ToolResult with the machine-readable code in
    ``data['code']`` so downstream callers (BFF, MCP ToolError envelopes)
    can switch on it without re-parsing the human string."""
    return ToolResult(
        success=False,
        error=message,
        data={"code": code, "status": "skipped"},
    )


# Re-export Optional at module level so _CopyResult's annotation resolves.
from typing import Optional  # noqa: E402  (local import keeps the source file's import block tidy)
