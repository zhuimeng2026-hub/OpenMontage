"""MiniMax (Hailuo AI) video generation via the direct MiniMax REST API.

Bypasses the fal.ai gateway: cheaper per-clip, no gateway markup, billed by
MiniMax directly. Supports the current MiniMax-Hailuo-2.3 family (pro / fast)
plus the older MiniMax-Hailuo-02 variants when the account has access.

Three-step flow:
    1. POST  /v1/video_generation                     -> task_id
    2. GET   /v1/query/video_generation?task_id=...   -> poll status
    3. GET   /v1/files/retrieve?file_id=...           -> download_url, then GET that URL

Known gotchas (verified live 2026-08):
    * `resolution` must be uppercase "768P" / "1080P" — "768p" returns 2013.
    * The submit response already carries task_id; do NOT re-poll on it.
    * Direct GET on /v1/files/{id}/content returns 403; only the
      /v1/files/retrieve?file_id=... -> download_url pattern works.
    * Status flow: Preparing -> Processing -> Success | Fail.
    * Real model name strings: "MiniMax-Hailuo-2.3" and "MiniMax-Hailuo-2.3-Fast".
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool,
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


SUBMIT_URL = "https://api.minimaxi.com/v1/video_generation"
POLL_URL = "https://api.minimaxi.com/v1/query/video_generation"
FILE_RETRIEVE_URL = "https://api.minimaxi.com/v1/files/retrieve"

# I2V reference image parameter name. The MiniMax API distinguishes
# text-to-video vs image-to-video by the presence of THIS field on submit,
# not by an `operation` flag and not by the older `image_url` alias. Without
# `first_frame_image` in the payload, the request is treated as T2V even
# when `image_url` is present — verified live 2026-08 with
# `MiniMax-Hailuo-2.3-Fast`, which rejected `image_url` payloads with
# "does not support Text-to-Video mode".
I2V_IMAGE_FIELD = "first_frame_image"
# Deprecated alias accepted for backward compatibility with earlier
# MiniMax-Hailuo-2.3 (non-Fast) submissions that succeeded via `image_url`.
# Will be removed in a future major version.
I2V_IMAGE_FIELD_LEGACY = "image_url"

# Terminal success / failure states returned by the poll endpoint.
_SUCCESS_STATES = frozenset({"Success", "Succeeded", "Finished", "Done"})
_FAILURE_STATES = frozenset({"Fail", "Failed", "Cancelled", "Canceled"})


class MiniMaxVideoDirect(BaseTool):
    name = "minimax_video_direct"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "minimax_direct"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC  # external call is async, we wrap with sync polling
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:MINIMAX_API_KEY"]
    install_instructions = (
        "Set MINIMAX_API_KEY in .env to your MiniMax (Hailuo AI) API key.\n"
        "  International: https://intl.minimaxi.com/\n"
        "  China:         https://api.minimaxi.com/"
    )
    agent_skills = ["minimax", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_image": True,
        "camera_direction": True,
        "prompt_optimizer": True,
    }
    best_for = [
        "direct MiniMax billing — no fal.ai gateway markup",
        "MiniMax-Hailuo-2.3 family (pro / fast) when a MiniMax API key is available",
        "high-texture footage with prompt-adherent camera motion",
        "Chinese-cloud provider with native Chinese-language prompt support",
    ]
    not_good_for = [
        "offline generation",
        "providers without a MiniMax account (use minimax_video via fal.ai or another backend)",
    ]
    fallback_tools = ["minimax_video", "kling_official_video", "veo_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the video. Supports Chinese.",
            },
            "model": {
                "type": "string",
                "enum": [
                    "MiniMax-Hailuo-2.3",
                    "MiniMax-Hailuo-2.3-Fast",
                ],
                "default": "MiniMax-Hailuo-2.3",
                "description": "MiniMax model name string.",
            },
            "duration": {
                "type": "integer",
                "enum": [6, 10],
                "default": 6,
                "description": "Clip duration in seconds. MiniMax-Hailuo-2.3 supports 6s and 10s.",
            },
            "resolution": {
                "type": "string",
                "enum": ["768P", "1080P"],
                "default": "768P",
                "description": "Output resolution. MUST be uppercase — '768p' returns base_resp 2013.",
            },
            "prompt_optimizer": {
                "type": "boolean",
                "default": True,
                "description": "Let MiniMax rewrite the prompt for better motion adherence.",
            },
            "first_frame_image": {
                "type": "string",
                "description": (
                    "Reference image URL for image_to-video. Required when operation is image_to_video; "
                    "the MiniMax API detects I2V mode by the presence of this field on submit. "
                    "Must be a publicly fetchable HTTPS URL (the model server downloads the image)."
                ),
            },
            "image_url": {
                "type": "string",
                "description": (
                    "DEPRECATED alias for `first_frame_image`. Accepted for backward compatibility "
                    "with earlier MiniMax-Hailuo-2.3 (non-Fast) submissions. Will be removed; "
                    "use `first_frame_image` instead. Note: `MiniMax-Hailuo-2.3-Fast` only "
                    "accepts `first_frame_image` and will reject this legacy alias."
                ),
            },
            "output_path": {
                "type": "string",
                "description": "Where to write the MP4. Parent directory is created if missing.",
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 60,
                "default": 5,
                "description": "Seconds between status polls.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 1800,
                "default": 600,
                "description": "Max time to wait for Success/Fail before giving up.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "duration", "resolution"]
    side_effects = ["writes MP4 to output_path", "calls api.minimaxi.com"]
    user_visible_verification = [
        "Watch generated clip for motion coherence, prompt adherence, and aspect ratio.",
        "Compare 768P vs 1080P cost/quality on a real brief before locking resolution in a pipeline.",
    ]

    # ------------------------------------------------------------------ auth

    def _get_api_key(self) -> str | None:
        return os.environ.get("MINIMAX_API_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_key() else ToolStatus.UNAVAILABLE

    # -------------------------------------------------------------- estimate

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Pricing for MiniMax-Hailuo-2.3 direct API is unverified at the time
        # this tool was written. Return 0.0 so the selector does not gate on
        # cost; callers should consult the MiniMax billing console.
        # https://intl.minimaxi.com/user-center/billing
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        # Observed 6s/768P clip: ~90s queue+gen. 10s/1080P can run 3-5 min.
        duration = int(inputs.get("duration", 6) or 6)
        resolution = str(inputs.get("resolution", "768P"))
        base = 90.0 if duration <= 6 else 180.0
        if resolution == "1080P":
            base *= 1.5
        return base

    # ----------------------------------------------------------------- exec

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="MINIMAX_API_KEY not set. " + self.install_instructions,
            )

        prompt = inputs["prompt"]
        model = inputs.get("model", "MiniMax-Hailuo-2.3")
        duration = int(inputs.get("duration", 6))
        resolution = str(inputs.get("resolution", "768P"))
        prompt_optimizer = bool(inputs.get("prompt_optimizer", True))
        output_path = Path(inputs.get("output_path", f"minimax_{int(time.time())}.mp4"))
        poll_interval = float(inputs.get("poll_interval_seconds", 5))
        timeout = int(inputs.get("timeout_seconds", 600))

        # Resolve the I2V reference image. `first_frame_image` is canonical;
        # `image_url` is a deprecated alias. Canonical wins when both are set.
        first_frame = inputs.get(I2V_IMAGE_FIELD) or inputs.get(I2V_IMAGE_FIELD_LEGACY)
        if inputs.get(I2V_IMAGE_FIELD_LEGACY) and not inputs.get(I2V_IMAGE_FIELD):
            print(
                f"[minimax_video_direct] WARNING: '{I2V_IMAGE_FIELD_LEGACY}' is deprecated; "
                f"pass '{I2V_IMAGE_FIELD}' instead. (Fast model requires it; standard model still "
                f"accepts the legacy alias as of 2026-08.)",
                flush=True,
            )

        # Map operation to endpoint; image_to_video uses the same endpoint
        # with first_frame_image attached.
        operation = "image_to_video" if first_frame else "text_to_video"

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "prompt_optimizer": prompt_optimizer,
        }
        if first_frame:
            payload[I2V_IMAGE_FIELD] = first_frame

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        start = time.time()
        try:
            # 1. Submit
            submit_resp = requests.post(SUBMIT_URL, headers=headers, json=payload, timeout=30)
            submit_resp.raise_for_status()
            submit_data = submit_resp.json()
            base_resp = submit_data.get("base_resp", {}) or {}
            if base_resp.get("status_code", 0) != 0:
                return ToolResult(
                    success=False,
                    error=(
                        f"MiniMax submit failed: {base_resp.get('status_msg', 'unknown')} "
                        f"(code={base_resp.get('status_code')})"
                    ),
                )
            task_id = submit_data.get("task_id")
            if not task_id:
                return ToolResult(
                    success=False,
                    error=f"MiniMax submit returned no task_id: {submit_data!r}",
                )

            # 2. Poll
            deadline = start + timeout
            last_status: str | None = None
            file_id: str | None = None
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll_resp = requests.get(
                    POLL_URL, headers=headers, params={"task_id": task_id}, timeout=20
                )
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
                status = poll_data.get("status", "UNKNOWN")
                if status != last_status:
                    # Log only on transition to keep noise down.
                    print(
                        f"[minimax_video_direct] task {task_id}: {status} "
                        f"file_id={poll_data.get('file_id', '')}",
                        flush=True,
                    )
                    last_status = status

                if status in _SUCCESS_STATES:
                    file_id = poll_data.get("file_id")
                    if not file_id:
                        return ToolResult(
                            success=False,
                            error=f"MiniMax Success without file_id: {poll_data!r}",
                        )
                    break
                if status in _FAILURE_STATES:
                    base_resp = poll_data.get("base_resp", {}) or {}
                    return ToolResult(
                        success=False,
                        error=(
                            f"MiniMax generation {status.lower()}: "
                            f"{base_resp.get('status_msg', 'unknown')} "
                            f"(code={base_resp.get('status_code')})"
                        ),
                    )
            else:
                return ToolResult(
                    success=False,
                    error=f"MiniMax generation timed out after {timeout}s (last status={last_status})",
                )

            # 3. Retrieve download URL
            retrieve_resp = requests.get(
                FILE_RETRIEVE_URL, headers=headers, params={"file_id": file_id}, timeout=30
            )
            retrieve_resp.raise_for_status()
            retrieve_data = retrieve_resp.json()
            download_url = (
                retrieve_data.get("download_url")
                or retrieve_data.get("file", {}).get("download_url")
            )
            if not download_url:
                return ToolResult(
                    success=False,
                    error=f"MiniMax file retrieve returned no download_url: {retrieve_data!r}",
                )

            # 4. Download
            video_resp = requests.get(download_url, timeout=300)
            video_resp.raise_for_status()

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_resp.content)

        except requests.RequestException as e:
            return ToolResult(success=False, error=f"MiniMax HTTP error: {e}")
        except Exception as e:  # last-resort safety net
            return ToolResult(success=False, error=f"MiniMax video generation failed: {e}")

        return ToolResult(
            success=True,
            data={
                "provider": "minimax_direct",
                "model": model,
                "operation": operation,
                "prompt": prompt,
                "duration": duration,
                "resolution": resolution,
                "task_id": task_id,
                "file_id": file_id,
                "output": str(output_path),
                "bytes_written": output_path.stat().st_size,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
