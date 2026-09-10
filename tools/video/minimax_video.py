"""MiniMax (Hailuo AI) video generation via fal.ai API.

Rewards prompt craft — follows camera directions well and produces high-texture footage.
"""

from __future__ import annotations

import logging
import os
import time
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
    ToolStatus,
    ToolTier,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Two channels, mirroring minimax_image.py:
#
# 1. ``_log`` (DEBUG) — opt-in via ``OM_DEBUG_VIDEO_GEN=1``. Used when
#    reproducing a fault. Dumps submit response keys, every poll status,
#    terminal failure payloads — every signal currently lost when
#    ToolResult.error is the only witness.
#
# 2. ``_detail_log`` (INFO) — always on. Emits one structured event per
#    generation through the ``openmontage.gen_detail`` logger, written to
#    ``logs/gen_detail.log`` when running under mcp_server.
_log = logging.getLogger(__name__)

_detail_log = logging.getLogger("openmontage.gen_detail")
if not _detail_log.handlers and not os.environ.get("OM_GEN_DETAIL_QUIET"):
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    _detail_log.addHandler(_h)
_detail_log.setLevel(logging.INFO)
_detail_log.propagate = False

if os.environ.get("OM_DEBUG_VIDEO_GEN") == "1":
    _log.setLevel(logging.DEBUG)
    if not _log.handlers:
        _dh = logging.StreamHandler()
        _dh.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(name)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        _log.addHandler(_dh)


def _redact(value: str) -> str:
    """Strip API keys / bearer tokens from a log line defensively."""
    import re as _re

    if not value:
        return value
    value = _re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", value)
    value = _re.sub(
        r"(?i)((?:api[_-]?key|token|cookie|authorization)\s*[:=]\s*)[^\s,;]+",
        r"\1<redacted>",
        value,
    )
    return value


def _prompt_snippet(prompt: str, limit: int = 80) -> str:
    if not prompt:
        return ""
    s = prompt.replace("\n", " ").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


class MiniMaxVideo(BaseTool):
    name = "minimax_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "minimax"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set FAL_KEY to your fal.ai API key.\n"
        "  Get one at https://fal.ai/dashboard/keys"
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "camera_direction": True,
    }
    best_for = [
        "prompt-following with camera directions (framing, motion, composition)",
        "high-texture footage with minimal hallucination",
        "cost-effective video generation",
    ]
    not_good_for = ["offline generation", "very long clips"]
    fallback_tools = ["kling_video", "veo_video", "wan_video"]

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
                "enum": [
                    "video-01", "hailuo-02/pro", "hailuo-02/standard",
                    "hailuo-2.3-fast/pro", "hailuo-2.3-fast/standard",
                ],
                "default": "hailuo-02/pro",
            },
            "image_url": {"type": "string", "description": "Reference image URL for image_to_video"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model_variant", "operation"]
    side_effects = ["writes video file to output_path", "calls fal.ai API"]
    user_visible_verification = ["Watch generated clip for motion coherence and prompt adherence"]

    def _get_api_key(self) -> str | None:
        return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")

    def get_status(self) -> ToolStatus:
        if self._get_api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "hailuo-02/pro")
        if "pro" in variant:
            return 0.15
        if "fast" in variant:
            return 0.08
        return 0.10  # standard

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "hailuo-02/pro")
        if "fast" in variant:
            return 30.0
        return 60.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="FAL_KEY not set. " + self.install_instructions,
            )

        import requests

        start = time.time()
        operation = inputs.get("operation", "text_to_video")
        variant = inputs.get("model_variant", "hailuo-02/pro")
        prompt_snippet = _prompt_snippet(inputs.get("prompt", ""))
        _log.debug(
            "minimax_video.execute enter operation=%s variant=%s prompt=%r image_url=%s",
            operation,
            variant,
            prompt_snippet,
            _redact(inputs.get("image_url") or ""),
        )
        _detail_log.info(
            "event=video_gen_detail state=submit tool=minimax_video provider=%s "
            "operation=%s model_variant=%s has_image_url=%s prompt=%r scene_id=%s",
            self.provider,
            operation,
            variant,
            bool(inputs.get("image_url")),
            prompt_snippet,
            inputs.get("scene_id"),
        )

        # Build fal.ai model path
        if operation == "text_to_video":
            model_path = f"minimax/{variant}/text-to-video"
            if variant == "video-01":
                model_path = "minimax/video-01"
        else:
            model_path = f"minimax/{variant}/image-to-video"
            if variant == "video-01":
                model_path = "minimax/video-01/image-to-video"

        payload: dict[str, Any] = {"prompt": inputs["prompt"]}
        if operation == "image_to_video" and inputs.get("image_url"):
            payload["image_url"] = inputs["image_url"]

        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }

        try:
            # Submit to queue API (async) — sync endpoint times out for video gen
            submit_resp = requests.post(
                f"https://queue.fal.run/fal-ai/{model_path}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            _log.debug(
                "minimax_video.execute submit http=%s elapsed_ms=%d",
                submit_resp.status_code,
                round((time.time() - start) * 1000),
            )
            submit_resp.raise_for_status()
            queue_data = submit_resp.json()
            status_url = queue_data.get("status_url")
            response_url = queue_data.get("response_url")
            _log.debug(
                "minimax_video.execute submit ok keys=%s has_status_url=%s has_response_url=%s",
                list(queue_data.keys()),
                bool(status_url),
                bool(response_url),
            )
            if not status_url or not response_url:
                _log.error(
                    "minimax_video.execute submit_response_incomplete keys=%s payload=%r",
                    list(queue_data.keys()),
                    _redact(repr(queue_data)[:300]),
                )
                _detail_log.info(
                    "event=video_gen_detail state=done tool=minimax_video success=false "
                    "reason=submit_response_incomplete duration_s=%.2f",
                    time.time() - start,
                )
                return ToolResult(
                    success=False,
                    error=f"fal.ai submit returned no status/response_url: {queue_data}",
                )

            # Poll until complete
            poll_idx = 0
            while True:
                poll_idx += 1
                time.sleep(5)
                status_resp = requests.get(status_url, headers=headers, timeout=15)
                status_resp.raise_for_status()
                status_payload = status_resp.json()
                status = status_payload.get("status", "UNKNOWN")
                _log.debug(
                    "minimax_video.execute poll=%d status=%s elapsed_s=%.1f keys=%s",
                    poll_idx,
                    status,
                    time.time() - start,
                    list(status_payload.keys()),
                )
                if status == "COMPLETED":
                    break
                if status in ("FAILED", "CANCELLED"):
                    _log.warning(
                        "minimax_video.execute terminal_status=%s payload=%r",
                        status,
                        _redact(repr(status_payload)[:400]),
                    )
                    _detail_log.info(
                        "event=video_gen_detail state=done tool=minimax_video success=false "
                        "reason=%s polls=%d duration_s=%.2f",
                        status.lower(),
                        poll_idx,
                        time.time() - start,
                    )
                    return ToolResult(
                        success=False,
                        error=f"MiniMax video generation {status.lower()}: "
                        f"{_redact(repr(status_payload)[:300])}",
                    )

            # Fetch result
            result_resp = requests.get(response_url, headers=headers, timeout=30)
            _log.debug(
                "minimax_video.execute fetch_result http=%s elapsed_s=%.1f",
                result_resp.status_code,
                time.time() - start,
            )
            result_resp.raise_for_status()
            data = result_resp.json()
            _log.debug(
                "minimax_video.execute result keys=%s has_video=%s",
                list(data.keys()),
                isinstance(data.get("video"), dict),
            )

            video_url = data["video"]["url"]
            video_response = requests.get(video_url, timeout=120)
            video_response.raise_for_status()
            _log.debug(
                "minimax_video.execute video_download bytes=%d", len(video_response.content)
            )

            output_path = Path(inputs.get("output_path", "minimax_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_response.content)

        except Exception as e:
            _log.error(
                "minimax_video.execute error=%s error_class=%s",
                _redact(str(e)),
                type(e).__name__,
            )
            _detail_log.info(
                "event=video_gen_detail state=done tool=minimax_video success=false "
                "reason=exception error_class=%s duration_s=%.2f",
                type(e).__name__,
                time.time() - start,
            )
            return ToolResult(success=False, error=f"MiniMax video generation failed: {e}")

        duration_s = round(time.time() - start, 2)
        _log.debug(
            "minimax_video.execute ok output=%s size=%d duration_s=%.2f",
            output_path,
            output_path.stat().st_size,
            duration_s,
        )
        _detail_log.info(
            "event=video_gen_detail state=done tool=minimax_video success=true "
            "model=%s cost_usd=%.4f polls=%d duration_s=%.2f",
            f"fal-ai/{model_path}",
            self.estimate_cost(inputs),
            poll_idx,
            duration_s,
        )
        return ToolResult(
            success=True,
            data={
                "provider": "minimax",
                "model": f"fal-ai/{model_path}",
                "prompt": inputs["prompt"],
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=duration_s,
            model=f"fal-ai/{model_path}",
        )
