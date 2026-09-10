"""MiniMax-H3 / H3-Max video generation via the kapon.cloud OneHub v2 proxy.

TEMPORARY bridge added 2026-09-10 while new-api routes H3 natively.
Delete the KAPON_* block from .env on the day new-api ships H3, and this
file can be retired.

Why kapon and not the official api.minimaxi.com?
    H3 / H3-Max are only exposed via MiniMax's v2 endpoint, which is not
    reachable from the standard `MINIMAX_API_KEY` (Token Plan) — that key
    is bound to v1 + Hailuo-2.3 / 02. The kapon OneHub proxy at
    `https://models.kapon.cloud/minimaxi/v2/video_generation` accepts the
    same v2 contract as MiniMax's official v2, but bills through a Bearer
    token the user owns on kapon.cloud. See decision_log d-009 in
    projects/xiaohongshu-neon-night-30s/artifacts/decision_log.json for
    the rationale.

Three-step flow (no separate file-retrieve step — content.url is returned
    directly from the poll response):
    1. POST {BASE_URL}/v2/video_generation                 -> task_id + platform_id
    2. GET  {BASE_URL}/v2/query/video_generation/{task_id} -> task.content.url (signed)
    3. GET  task.content.url                               -> MP4 bytes

Capability matrix per kapon docs (verified 2026-09-10):
    MiniMax-H3:      768P / 2K, 4–15s, supports T2V / I2V / first+last frame /
                     multi-image reference / reference video / reference audio
    MiniMax-H3-Max:  480P / 768P, 5–15s, supports T2V / I2V / first+last frame
                     ONLY. Rejects any reference_* media. This tool enforces
                     that — passing reference_* with H3-Max returns a hard error.

Pricing (from platform.minimaxi.com paygo page, fetched 2026-09-10):
    MiniMax-H3 768P:     ¥0.50 / second  (output only)
    MiniMax-H3 2K:       ¥0.80 / second  (output only)
    MiniMax-H3-Max 480P: ¥0.33 / second  (output only, cheapest)
    MiniMax-H3-Max 768P: ¥0.50 / second  (output only)
    H3 input images: 5 free, then ¥0.20/image.
    H3-Max input images: free (only output billed).
    kapon does not declare a markup in its docs; use the same ¥/s as the
    official paygo price. Cost estimate in this tool uses those rates.
"""

from __future__ import annotations

import os
import re
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


# --- model capability matrix (mirrors kapon docs verbatim) -----------------

_MODEL_RESOLUTIONS = {
    "MiniMax-H3":     ("768P", "2K"),
    "MiniMax-H3-Max": ("480P", "768P"),
}
_MODEL_DURATION_RANGE = {
    "MiniMax-H3":     (4, 15),
    "MiniMax-H3-Max": (5, 15),
}
_H3_MAX_FORBIDDEN_FIELDS = (
    "subject_reference",
    "reference_image",
    "reference_video",
    "reference_audio",
)
# Only H3 supports reference_* media. H3-Max only T2V / I2V / first+last frame.
# First+last frame is encoded as image_url with role="first_frame" / "last_frame"
# inside content[] — same shape as I2V, so H3-Max accepts them naturally.

# kapon-allowed aspect ratios for ratio field. 'adaptive' is allowed only in
# first/last-frame and reference mode (we validate per-mode in execute()).
_RATIOS_CONCRETE = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


class MiniMaxH3Video(BaseTool):
    name = "minimax_h3_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "minimax_h3_kapon"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC  # external call is async, we wrap with sync polling
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:KAPON_API_TOKEN", "env:KAPON_VIDEO_BASE_URL"]
    install_instructions = (
        "Set KAPON_API_TOKEN + KAPON_VIDEO_BASE_URL in .env.\n"
        "  KAPON_VIDEO_BASE_URL defaults to https://models.kapon.cloud/minimaxi\n"
        "  Get a token at https://docs.kapon.cloud (console → API 令牌, model-call scope).\n"
        "TEMPORARY: delete the KAPON_* .env block once new-api routes H3 natively."
    )
    agent_skills = ["minimax", "ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        # Multi-image reference / reference_video / reference_audio are
        # supported only on MiniMax-H3 (NOT H3-Max). The execute() method
        # hard-rejects these on H3-Max.
        "reference_image": "model-conditional",
        "reference_video": "model-conditional",
        "reference_audio": "model-conditional",
    }
    best_for = [
        "MiniMax-H3 / H3-Max generations when new-api does not yet route H3",
        "I2V / first+last frame / T2V across both H3 and H3-Max",
        "5–15s clips at 480P / 768P / 2K (model-dependent)",
        "Independent billing pool — does NOT consume MINIMAX_API_KEY / Hailuo Token Plan",
    ]
    not_good_for = [
        "Multi-image reference or reference_video on H3-Max (rejected)",
        "Anything beyond H3 / H3-Max (use minimax_video_direct for Hailuo-2.3 / 02)",
        "Long-running production without explicit user approval — temporary bridge",
    ]
    fallback_tools = ["minimax_video_direct", "minimax_video", "kling_official_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 7000,
                "description": (
                    "Text prompt for the video. Up to 7000 Unicode characters. "
                    "Chinese supported natively."
                ),
            },
            "model": {
                "type": "string",
                "enum": ["MiniMax-H3", "MiniMax-H3-Max"],
                "default": "MiniMax-H3-Max",
                "description": (
                    "MiniMax model name. H3 supports reference_* media; H3-Max is "
                    "T2V / I2V / first+last-frame only."
                ),
            },
            "duration": {
                "type": "integer",
                "minimum": 4,
                "maximum": 15,
                "default": 6,
                "description": "Clip duration in seconds (H3: 4–15, H3-Max: 5–15).",
            },
            "resolution": {
                "type": "string",
                "enum": ["480P", "768P", "2K"],
                "default": "768P",
                "description": (
                    "Output resolution. 480P and 2K are model-restricted: "
                    "480P only on H3-Max; 2K only on H3. Tool validates."
                ),
            },
            "ratio": {
                "type": "string",
                "enum": [*_RATIOS_CONCRETE, "adaptive"],
                "description": (
                    "Aspect ratio. REQUIRED for pure T2V (no media items) and "
                    "MUST be a concrete ratio (not 'adaptive'). For I2V / "
                    "first+last frame, defaults to 'adaptive'. For reference "
                    "modes, defaults to 'adaptive' but can be overridden."
                ),
            },
            "first_frame_image": {
                "type": "string",
                "description": (
                    "Public HTTPS URL, data URI, or mm_file://<int64> locator "
                    "for I2V / first+last-frame mode. kapon fetches and "
                    "validates size/MIME."
                ),
            },
            "last_frame_image": {
                "type": "string",
                "description": "Same locator rules as first_frame_image. H3-Max supported.",
            },
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 9,
                "description": (
                    "Up to 9 reference image URLs for multi-image reference "
                    "mode. H3 ONLY — H3-Max rejects. Cannot combine with "
                    "first/last frame."
                ),
            },
            "reference_videos": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Up to 3 reference video URLs. H3 ONLY.",
            },
            "reference_audios": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
                "description": "Up to 3 reference audio URLs. H3 ONLY. Must combine with reference_image or reference_video.",
            },
            "output_path": {
                "type": "string",
                "description": "Where to write the MP4. Parent directory created if missing.",
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
                "description": "Max wait time before giving up (default 10 min).",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "duration", "resolution"]
    side_effects = ["writes MP4 to output_path", "calls models.kapon.cloud"]
    user_visible_verification = [
        "Watch the generated clip for motion coherence, prompt adherence, and ratio.",
        "Compare H3-Max 480P vs H3 768P cost/quality before locking resolution.",
        "Verify model-conditional support: reference_* only works on H3.",
    ]

    # ------------------------------------------------------------------ auth

    def _get_api_token(self) -> str | None:
        return os.environ.get("KAPON_API_TOKEN")

    def _get_base_url(self) -> str:
        # Default per kapon docs (verified 2026-09-10).
        return os.environ.get("KAPON_VIDEO_BASE_URL", "https://models.kapon.cloud/minimaxi").rstrip("/")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_token() else ToolStatus.UNAVAILABLE

    # -------------------------------------------------------------- estimate

    # Per-second output prices from platform.minimaxi.com/docs/guides/pricing-paygo.
    # Confirmed 2026-09-10. Multiply by duration to estimate.
    _OUTPUT_PRICE_PER_SEC = {
        ("MiniMax-H3",     "768P"): 0.50,
        ("MiniMax-H3",     "2K"):   0.80,
        ("MiniMax-H3-Max", "480P"): 0.33,
        ("MiniMax-H3-Max", "768P"): 0.50,
    }

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Return estimated cost in CNY (¥). Output-only, H3/H3-Max paygo rates."""
        model = inputs.get("model", "MiniMax-H3-Max")
        resolution = str(inputs.get("resolution", "768P"))
        duration = int(inputs.get("duration", 6) or 6)
        rate = self._OUTPUT_PRICE_PER_SEC.get((model, resolution), 0.0)
        return round(rate * duration, 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        # H3 observed: 4–6s clips ~60s, 10s ~120s, 15s ~180s. H3-Max is faster.
        duration = int(inputs.get("duration", 6) or 6)
        resolution = str(inputs.get("resolution", "768P"))
        model = inputs.get("model", "MiniMax-H3-Max")
        base = 30.0 if duration <= 6 else 90.0
        if resolution == "2K":
            base *= 1.5
        if model == "MiniMax-H3-Max":
            base *= 0.7  # H3-Max is the "fast" variant, cheaper per docs
        return base

    # ------------------------------------------------------------- validation

    def _validate_inputs(self, inputs: dict[str, Any]) -> str | None:
        """Return None if valid, else an error message describing the violation."""
        model = inputs.get("model", "MiniMax-H3-Max")
        resolution = str(inputs.get("resolution", "768P"))
        duration = int(inputs.get("duration", 6) or 6)
        ratio = inputs.get("ratio")
        first = inputs.get("first_frame_image")
        last = inputs.get("last_frame_image")
        refs_img = inputs.get("reference_images") or []
        refs_vid = inputs.get("reference_videos") or []
        refs_aud = inputs.get("reference_audios") or []

        if model not in _MODEL_RESOLUTIONS:
            return f"unsupported model: {model}"
        allowed_res = _MODEL_RESOLUTIONS[model]
        if resolution not in allowed_res:
            return (
                f"resolution {resolution} not allowed for {model}; "
                f"allowed: {','.join(allowed_res)}"
            )

        dmin, dmax = _MODEL_DURATION_RANGE[model]
        if not (dmin <= duration <= dmax):
            return f"duration {duration}s out of range for {model}: {dmin}–{dmax}"

        # H3-Max hard-rejects reference_* media
        if model == "MiniMax-H3-Max":
            if refs_img or refs_vid or refs_aud:
                return (
                    "MiniMax-H3-Max does not support reference_image / reference_video / "
                    "reference_audio. Per kapon docs: 'H3-Max 只接受文本和首/尾帧图片，"
                    "不接受参考素材'. Use MiniMax-H3 if you need reference_*."
                )

        # kapon rule: first+last frame cannot coexist with reference_* media
        if (first or last) and (refs_img or refs_vid or refs_aud):
            return "first/last frame mode cannot be combined with reference_* media"

        # kapon rule: if ONLY reference_audio, must also have reference_image or reference_video
        if refs_aud and not (refs_img or refs_vid):
            return "reference_audio requires at least one reference_image or reference_video"

        # Ratio semantics
        is_t2v = not (first or last or refs_img or refs_vid or refs_aud)
        if is_t2v:
            if not ratio:
                return "T2V mode (no media items) requires `ratio` field"
            if ratio == "adaptive":
                return "T2V mode does not accept ratio='adaptive'; use a concrete ratio"
        return None

    # ----------------------------------------------------------------- exec

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        token = self._get_api_token()
        if not token:
            return ToolResult(
                success=False,
                error="KAPON_API_TOKEN not set. " + self.install_instructions,
            )

        validation_err = self._validate_inputs(inputs)
        if validation_err:
            return ToolResult(success=False, error=f"input validation: {validation_err}")

        base = self._get_base_url()
        prompt = inputs["prompt"]
        model = inputs["model"]
        duration = int(inputs["duration"])
        resolution = str(inputs["resolution"])
        first = inputs.get("first_frame_image")
        last = inputs.get("last_frame_image")
        refs_img = inputs.get("reference_images") or []
        refs_vid = inputs.get("reference_videos") or []
        refs_aud = inputs.get("reference_audios") or []
        explicit_ratio = inputs.get("ratio")

        output_path = Path(inputs.get("output_path", f"minimax_h3_{int(time.time())}.mp4"))
        poll_interval = float(inputs.get("poll_interval_seconds", 5))
        timeout = int(inputs.get("timeout_seconds", 600))

        # Build content[] per kapon v2 contract. Exactly one text item, then
        # media items per mode.
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

        def _img_loc(url: str, role: str | None) -> dict[str, Any]:
            # kapon accepts image_url with role (first_frame / last_frame).
            # image_url without role is treated as first_frame by default.
            entry: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
            if role:
                entry["image_url"]["role"] = role
            return entry

        if first:
            content.append(_img_loc(first, "first_frame"))
        if last:
            content.append(_img_loc(last, "last_frame"))
        for u in refs_img:
            content.append({"type": "image_url", "image_url": {"url": u, "role": "reference_image"}})
        for u in refs_vid:
            content.append({"type": "video_url", "video_url": {"url": u, "role": "reference_video"}})
        for u in refs_aud:
            content.append({"type": "audio_url", "audio_url": {"url": u, "role": "reference_audio"}})

        # Ratio: T2V already validated to require concrete ratio. Other modes
        # default to adaptive if not specified.
        has_media = bool(first or last or refs_img or refs_vid or refs_aud)
        if explicit_ratio:
            ratio = explicit_ratio
        elif has_media:
            ratio = "adaptive"
        else:
            # Should not happen: validation already failed T2V without ratio.
            ratio = "16:9"

        payload: dict[str, Any] = {
            "model": model,
            "content": content,
            "duration": duration,
            "resolution": resolution,
            "ratio": ratio,
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        submit_url = f"{base}/v2/video_generation"
        # kapon returns task_id + platform_id. base_resp status_code=0 means OK.

        start = time.time()
        try:
            # 1. Submit
            submit_resp = requests.post(submit_url, headers=headers, json=payload, timeout=30)
            submit_resp.raise_for_status()
            submit_data = submit_resp.json()
            base_resp = submit_data.get("base_resp", {}) or {}
            if base_resp.get("status_code", 0) != 0:
                return ToolResult(
                    success=False,
                    error=(
                        f"kapon submit failed: {base_resp.get('status_msg', 'unknown')} "
                        f"(code={base_resp.get('status_code')})"
                    ),
                )
            task_id = submit_data.get("task_id")
            platform_id = submit_data.get("platform_id")
            if not task_id:
                return ToolResult(
                    success=False,
                    error=f"kapon submit returned no task_id: {submit_data!r}",
                )

            # 2. Poll. Poll URL has task_id as path param (NOT query string).
            deadline = start + timeout
            last_status: str | None = None
            content_url: str | None = None
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll_resp = requests.get(
                    f"{base}/v2/query/video_generation/{task_id}",
                    headers=headers,
                    timeout=20,
                )
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
                # kapon wraps task fields under "task"
                task_obj = poll_data.get("task") or poll_data
                status = task_obj.get("status", "UNKNOWN")
                if status != last_status:
                    print(
                        f"[minimax_h3_video] task {task_id} platform {platform_id}: {status}",
                        flush=True,
                    )
                    last_status = status

                if status in ("succeeded", "Success", "Succeeded", "Finished", "Done"):
                    content_url = (
                        task_obj.get("content", {}).get("url")
                        if isinstance(task_obj.get("content"), dict)
                        else None
                    )
                    if not content_url:
                        return ToolResult(
                            success=False,
                            error=f"kapon succeeded but no content.url: {poll_data!r}",
                        )
                    break
                if status in ("failed", "Failed", "Fail", "Cancelled", "Canceled"):
                    err = task_obj.get("error") or {}
                    return ToolResult(
                        success=False,
                        error=(
                            f"kapon generation {status}: "
                            f"{err.get('message', err) if isinstance(err, dict) else err}"
                        ),
                    )
            else:
                return ToolResult(
                    success=False,
                    error=f"kapon generation timed out after {timeout}s (last status={last_status})",
                )

            # 3. Download signed URL. kapon warns: this URL expires; do NOT log.
            video_resp = requests.get(content_url, timeout=300)
            video_resp.raise_for_status()

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_resp.content)

        except requests.RequestException as e:
            body_snippet = ""
            try:
                if e.response is not None:
                    body_snippet = f" | kapon body: {e.response.text[:500]}"
            except Exception:
                pass
            return ToolResult(success=False, error=f"kapon HTTP error: {e}{body_snippet}")
        except Exception as e:  # last-resort safety net
            return ToolResult(success=False, error=f"minimax_h3_video failed: {e}")

        return ToolResult(
            success=True,
            data={
                "provider": "minimax_h3_kapon",
                "model": model,
                "prompt": prompt,
                "duration": duration,
                "resolution": resolution,
                "ratio": ratio,
                "task_id": task_id,
                "platform_id": platform_id,
                "output": str(output_path),
                "bytes_written": output_path.stat().st_size,
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
