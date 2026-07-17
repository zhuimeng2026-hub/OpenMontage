"""Alibaba Cloud DashScope (通义万相 - Tongyi Wanxiang) image generation."""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
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


class DashScopeImage(BaseTool):
    name = "dashscope_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "dashscope"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set DASHSCOPE_KEY or DashScope_key to your Alibaba Cloud DashScope API key.\n"
        "  Get one at https://dashscope.aliyuncs.com/"
    )

    capabilities = ["generate_image", "text_to_image", "image_to_image", "generate_illustration"]
    supports = {
        "negative_prompt": False,
        "seed": False,
        "custom_size": True,
        "image_to_image": True,
    }
    best_for = [
        "Chinese-themed image generation",
        "cost-effective general image generation",
        "multi-style image generation (animé, oil painting, sketch)",
        "image-to-image transformation and editing",
    ]
    not_good_for = ["photorealistic faces", "offline generation"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image or transformation instruction"},
            "image_path": {"type": "string", "description": "Source image path (local file) for image-to-image transformation"},
            "size": {
                "type": "string",
                "enum": ["1024*1024", "720*1280", "1280*720", "768*1152", "1152*768"],
                "default": "1024*1024",
            },
            "style": {
                "type": "string",
                "enum": ["<auto>", "<animé>", "<oil-painting>", "<watercolor>", "<sketch>", "<chinese-water-ink>", "<3d-model>"],
                "default": "<auto>",
                "description": "Image style (use <auto> for automatic)",
            },
            "n": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
                "maximum": 4,
                "description": "Number of images to generate",
            },
            "output_path": {"type": "string", "description": "Local path to save the generated image"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "size", "style"]
    side_effects = ["writes image file to output_path", "calls Alibaba Cloud DashScope API"]
    user_visible_verification = ["Inspect generated image for relevance and quality"]

    def _get_api_key(self) -> str | None:
        return os.environ.get("DASHSCOPE_KEY") or os.environ.get("DashScope_key") or os.environ.get("DASHSCOPE_API_KEY")

    def _get_api_url(self) -> str:
        return os.environ.get("DASHSCOPE_URL", "https://dashscope.aliyuncs.com")

    def get_status(self) -> ToolStatus:
        if self._get_api_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.02  # ~0.02 USD per image

    def _call_api(self, api_url: str, payload: dict, api_key: str, timeout: int = 120) -> dict:
        """Submit async task to DashScope API and return response with task_id."""
        import urllib.request as urllib_req
        import urllib.error as urllib_err

        req = urllib_req.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            method="POST",
        )
        with urllib_req.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _poll_task(self, task_id: str, api_key: str, base_url: str, timeout: int = 120) -> dict | None:
        """Poll DashScope task until completion or timeout."""
        import urllib.request as urllib_req
        import urllib.error as urllib_err

        poll_url = f"{base_url}/api/v1/tasks/{task_id}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            req = urllib_req.Request(
                poll_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib_req.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            output = body.get("output", {})
            status = output.get("task_status", "")
            if status == "SUCCEEDED":
                return body
            if status in ("FAILED", "CANCELED"):
                return None
            time.sleep(2)
        return None  # timeout

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="No DashScope API key found. " + self.install_instructions,
            )

        start = time.time()
        prompt = inputs["prompt"]
        size = inputs.get("size", "1024*1024")
        style = inputs.get("style", "<auto>")
        n = min(inputs.get("n", 1), 4)

        # Support both image_path and source_image (pipeline alias)
        source_image = inputs.get("image_path") or inputs.get("source_image", "")
        base_url = self._get_api_url()

        # wanx-v1 uses text2image endpoint for both text-to-image and image-to-image.
        # Image-to-image via input.image_url (public URL required).
        api_url = f"{base_url}/api/v1/services/aigc/text2image/image-synthesis"
        payload: dict[str, Any] = {
            "model": "wanx-v1",
            "input": {
                "prompt": prompt,
            },
            "parameters": {
                "n": n,
                "size": size,
            },
        }

        if source_image:
            if source_image.startswith(("http://", "https://")):
                payload["input"]["image_url"] = source_image
            else:
                return ToolResult(
                    success=False,
                    error=(
                        "dashscope_image image-to-image needs a public image URL. "
                        "Local files not supported. Provide http(s) URL or use grok_image. "
                        f"Path: {source_image}"
                    ),
                    duration_seconds=time.time() - start,
                )
        if style and style != "<auto>":
            payload["parameters"]["style"] = style

        # Step 1: Submit async task
        try:
            # Use async mode (DashScope requires async for this API key)
            body = self._call_api(api_url, payload, api_key)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            return ToolResult(
                success=False,
                error=f"DashScope API HTTP {e.code}: {error_body}",
                duration_seconds=time.time() - start,
            )
        except urllib.error.URLError as e:
            return ToolResult(
                success=False,
                error=f"DashScope API connection error: {e.reason}",
                duration_seconds=time.time() - start,
            )

        # Check for API-level errors
        if body.get("code"):
            return ToolResult(
                success=False,
                error=f"DashScope API error: {body.get('code')} - {body.get('message', '')}",
                duration_seconds=time.time() - start,
            )

        # Step 2: Get task_id from response
        output = body.get("output", {})
        task_id = output.get("task_id", "")
        results = output.get("results", [])

        # If results already present (sync mode), use them directly
        if results:
            return self._process_results(results, inputs, start)

        # Otherwise poll for async completion
        if not task_id:
            return ToolResult(
                success=False,
                error="DashScope returned no task_id and no results",
                duration_seconds=time.time() - start,
            )

        poll_result = self._poll_task(task_id, api_key, base_url)
        if poll_result is None:
            return ToolResult(
                success=False,
                error=f"DashScope task {task_id} failed or timed out",
                duration_seconds=time.time() - start,
            )

        results = poll_result.get("output", {}).get("results", [])
        if not results:
            return ToolResult(
                success=False,
                error=f"DashScope task {task_id} completed but no image results",
                duration_seconds=time.time() - start,
            )

        return self._process_results(results, inputs, start)

    def _process_results(self, results: list, inputs: dict, start: float) -> ToolResult:
        """Download images from results and return ToolResult."""
        image_url = results[0].get("url", "")
        if not image_url:
            return ToolResult(
                success=False,
                error="DashScope result missing image URL",
                duration_seconds=time.time() - start,
            )

        # Determine output path
        output_path = inputs.get("output_path", "")
        if output_path:
            dest = Path(output_path)
        else:
            base = Path(os.environ.get("PIPELINE_WORK_DIR", "/tmp"))
            dest = base / f"dashscope_{int(time.time())}.png"

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Download the image
        try:
            img_req = urllib.request.Request(image_url, headers={"User-Agent": "OpenMontage/1.0"})
            with urllib.request.urlopen(img_req, timeout=60) as img_resp:
                dest.write_bytes(img_resp.read())
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Failed to download generated image: {e}",
                duration_seconds=time.time() - start,
            )

        elapsed = time.time() - start
        return ToolResult(
            success=True,
            data={
                "image_path": str(dest),
                "image_url": image_url,
            },
            duration_seconds=elapsed,
            cost_usd=self.estimate_cost(inputs),
            artifacts=[str(dest)],
        )
