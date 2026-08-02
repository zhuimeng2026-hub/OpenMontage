"""Seedance 2.0 (ByteDance) video generation via a new-api compatible relay (中转站).

Reference seedance_video.py for the fal.ai path. This tool talks to a new-api
compatible relay endpoint (new-api / one-api aggregator) which already bundles
Seedance(豆包) upstream channels, so OpenMontage only needs the thin
submit -> poll -> download client implemented in tools/video/_relay.py.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.video._relay import RelayError, generate_via_relay


class SeedanceRelay(BaseTool):
    name = "seedance_relay"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "seedance_relay"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:VIDEO_RELAY_BASE_URL", "env:VIDEO_RELAY_API_KEY"]
    install_instructions = (
        "Set VIDEO_RELAY_BASE_URL to your new-api / 中转站 endpoint "
        "(e.g. http://127.0.0.1:3000) and VIDEO_RELAY_API_KEY to your "
        "中转站 access token. Both go in .env."
    )
    agent_skills = ["seedance-2-0", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "native_audio": True,
        "cinematic_quality": True,
        "camera_direction": True,
        "lip_sync": True,
        "multi_shot": True,
        "aspect_ratio": True,
        "seed": True,
    }
    best_for = [
        "Seedance 2.0 video generation via a self-hosted new-api compatible relay",
        "cinematic clips with native audio when a 中转站 is preferred over fal.ai",
        "budget-conscious projects (relay pricing is usually lower than fal.ai)",
    ]
    not_good_for = ["offline generation", "direct fal.ai usage"]
    fallback_tools = [
        "seedance_video",
        "seedance_replicate",
        "kling_video",
        "kling_relay",
        "veo_video",
        "minimax_video",
    ]

    MODEL_MAP = {
        "standard": "seedance-2-0",
        "fast": "seedance-2-0-fast",
    }
    DEFAULT_MODEL = "seedance-2-0"

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model_variant": {
                "type": "string",
                "enum": ["standard", "fast"],
                "default": "standard",
                "description": "standard = highest quality, fast = lower latency and cost",
            },
            "model_name": {
                "type": "string",
                "description": "Optional raw new-api model id; directly overrides the relay model, skipping MODEL_MAP.",
            },
            "duration": {
                "type": "string",
                "enum": ["5", "10"],
                "default": "5",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "default": "16:9",
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p"],
                "default": "720p",
            },
            "generate_audio": {
                "type": "boolean",
                "default": True,
                "description": "Generate synchronized audio (speech, SFX, ambient)",
            },
            "image_url": {
                "type": "string",
                "description": "Start frame image URL for image_to_video (public URL; the relay path does not upload local files)",
            },
            "seed": {
                "type": "integer",
                "description": "Optional seed for reproducibility (passed into relay metadata)",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model_variant", "operation", "duration", "seed"]
    side_effects = [
        "calls relay API (new-api 中转站)",
        "writes video file to output_path",
    ]
    user_visible_verification = [
        "Watch generated clip for motion coherence, audio sync, and visual quality"
    ]

    def _get_base_url(self) -> str | None:
        return os.environ.get("VIDEO_RELAY_BASE_URL")

    def _get_api_key(self) -> str | None:
        return os.environ.get("VIDEO_RELAY_API_KEY")

    def get_status(self) -> ToolStatus:
        if self._get_base_url() and self._get_api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "standard")
        duration = inputs.get("duration", "5")
        secs = int(duration)
        rate = 0.20 if variant == "fast" else 0.25
        return round(rate * secs, 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 120.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        start = time.time()
        operation = inputs.get("operation", "text_to_video")
        variant = inputs.get("model_variant", "standard")
        model = str(inputs.get("model_name") or self.MODEL_MAP.get(variant, self.DEFAULT_MODEL))

        metadata: dict[str, Any] = {}
        if inputs.get("aspect_ratio"):
            metadata["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            metadata["resolution"] = inputs["resolution"]
        if "generate_audio" in inputs:
            metadata["generate_audio"] = inputs["generate_audio"]
        if inputs.get("seed") is not None:
            metadata["seed"] = inputs["seed"]

        output_path = inputs.get("output_path", "seedance_relay_output.mp4")

        try:
            result = generate_via_relay(
                base_url=self._get_base_url(),
                api_key=self._get_api_key(),
                model=model,
                prompt=inputs["prompt"],
                operation=operation,
                image_url=inputs.get("image_url") if operation == "image_to_video" else None,
                duration=float(inputs.get("duration", "5")),
                metadata=metadata or None,
                output_path=output_path,
            )
        except RelayError as exc:
            return ToolResult(
                success=False,
                error=f"Seedance relay video generation failed: {exc}",
            )

        return ToolResult(
            success=True,
            data={
                **result,
                "provider": "seedance_relay",
                "prompt": inputs["prompt"],
                "operation": operation,
                "model_variant": variant,
                "resolution": inputs.get("resolution", "720p"),
                "generate_audio": inputs.get("generate_audio", True),
            },
            artifacts=[str(result["output_path"])],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
