"""Kling video generation via a new-api compatible relay (中转站).

Reference kling_video.py fal.ai path. Instead of calling fal.ai directly, this
tool submits the generation job to a new-api / 中转站 endpoint using the shared
relay client (tools.video._relay) and downloads the resulting mp4.
"""

from __future__ import annotations

import os
import time
from typing import Any

from tools.video import _relay
from tools.video._relay import RelayError
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


class KlingRelay(BaseTool):
    name = "kling_relay"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "kling_relay"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:VIDEO_RELAY_BASE_URL", "env:VIDEO_RELAY_API_KEY"]
    install_instructions = (
        "Set VIDEO_RELAY_BASE_URL to your new-api / 中转站 endpoint, "
        "e.g. http://127.0.0.1:3000\n"
        "Set VIDEO_RELAY_API_KEY to your 中转站 access token."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "native_audio": True,
        "cinematic_quality": True,
    }
    best_for = [
        "cinematic B-roll via a new-api relay (中转站)",
        "Kling models at relay pricing",
        "fluid motion and camera direction",
    ]
    not_good_for = [
        "offline generation",
        "projects without a new-api relay endpoint",
        "direct fal.ai Kling access",
    ]
    fallback_tools = [
        "kling_video",
        "kling_official_video",
        "seedance_video",
        "seedance_relay",
        "veo_video",
        "minimax_video",
    ]

    MODEL_MAP = {
        "v2.1/master": "kling-v2-master",
        "v2.1/pro": "kling-v1-6",
        "v2.1/standard": "kling-v1",
        "v3/standard": "kling-v1-6",
    }
    DEFAULT_MODEL = "kling-v2-master"

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
                "enum": ["v2.1/master", "v2.1/pro", "v2.1/standard", "v3/standard"],
                "default": "v2.1/master",
                "description": "OpenMontage variant; maps via MODEL_MAP",
            },
            "model_name": {
                "type": "string",
                "description": "Directly overrides the new-api model, skipping MODEL_MAP",
            },
            "duration": {
                "type": "string",
                "enum": ["5", "10"],
                "default": "5",
                "description": "Duration in seconds",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "default": "16:9",
            },
            "negative_prompt": {"type": "string"},
            "mode": {"type": "string", "default": "std"},
            "cfg_scale": {"type": "number"},
            "image_url": {"type": "string", "description": "Reference image URL for image_to_video"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model_variant", "operation", "duration"]
    side_effects = ["calls relay API (new-api 中转站)", "writes video file to output_path"]
    user_visible_verification = ["Watch generated clip for motion coherence and visual quality"]

    def _get_base_url(self) -> str | None:
        return os.environ.get("VIDEO_RELAY_BASE_URL")

    def _get_api_key(self) -> str | None:
        return os.environ.get("VIDEO_RELAY_API_KEY")

    def get_status(self) -> ToolStatus:
        try:
            self.check_dependencies()
            return ToolStatus.AVAILABLE
        except DependencyError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "v2.1/master")
        duration = int(inputs.get("duration", "5"))
        if "master" in variant:
            return 0.24 * (duration / 5)
        if "pro" in variant:
            return 0.16 * (duration / 5)
        return 0.08 * (duration / 5)  # standard

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 60.0  # ~1 minute typical

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
        except DependencyError as exc:
            return ToolResult(success=False, error=str(exc))

        start = time.time()
        operation = inputs.get("operation", "text_to_video")
        resolved_model = inputs.get("model_name") or self.MODEL_MAP.get(
            inputs.get("model_variant"), self.DEFAULT_MODEL
        )

        metadata: dict[str, Any] = {}
        if inputs.get("aspect_ratio"):
            metadata["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("mode"):
            metadata["mode"] = inputs["mode"]
        if inputs.get("negative_prompt"):
            metadata["negative_prompt"] = inputs["negative_prompt"]
        if inputs.get("cfg_scale") is not None:
            metadata["cfg_scale"] = inputs["cfg_scale"]

        try:
            result = _relay.generate_via_relay(
                base_url=self._get_base_url() or "",
                api_key=self._get_api_key() or "",
                model=resolved_model,
                prompt=inputs["prompt"],
                operation=operation,
                image_url=inputs.get("image_url"),
                duration=float(inputs["duration"]) if inputs.get("duration") else None,
                metadata=metadata or None,
                output_path=inputs.get("output_path", "kling_relay_output.mp4"),
                poll_interval=5.0,
                poll_timeout=900.0,
            )
        except RelayError as exc:
            return ToolResult(
                success=False,
                error=f"Kling relay video generation failed: {exc}",
            )

        output_path = result["output_path"]
        return ToolResult(
            success=True,
            data={
                **result,
                "provider": "kling_relay",
                "prompt": inputs["prompt"],
                "operation": operation,
                "model_variant": inputs.get("model_variant"),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=resolved_model,
        )
