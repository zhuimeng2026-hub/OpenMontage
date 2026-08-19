"""MiniMax (Hailuo AI) image generation via direct API — Image-01 model.

⚠️  EXPERIMENTAL — endpoint, response shapes, and pricing are unverified
in this initial implementation. See `.agents/skills/minimax/SKILL.md` for
the verification checklist. The tool fails loudly on unrecognized response
shapes so the next maintainer can extend `_parse_response()` rather than
silently producing wrong output.

The response parser handles three formats seen in modern image-gen APIs:
  - sync base64 inline: `{data: [{b64_json: ...}]}` (OpenAI-style)
  - sync URL list:     `{images: [{url: ...}]}` (Replicate/fal-style)
  - async task poll:   `{task_id: ...}` then poll status URL until done

PIL is used to verify each downloaded image is parseable, and to flatten
RGBA → RGB on a white background so JPEG output doesn't fail.
"""

from __future__ import annotations

import base64
import io
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


class MiniMaxImage(BaseTool):
    name = "minimax_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "minimax"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set MINIMAX_API_KEY to your MiniMax API key.\n"
        "  Get one at https://intl.minimaxi.com/"
    )
    fallback_tools = [
        "flux_image", "openai_image", "recraft_image",
        "dashscope_image", "grok_image",
    ]
    agent_skills = ["minimax"]

    capabilities = ["generate_image", "text_to_image"]
    supports = {
        "multiple_outputs": True,
        "aspect_ratio": True,
        "seed": True,
        "transparent_png": True,  # via PIL RGBA path
    }
    best_for = [
        "cost-effective text-to-image (claimed ~1/10 of comparable models)",
        "Chinese-language prompts (MiniMax is a Chinese-cloud provider)",
        "high-fidelity prompt adherence",
    ]
    not_good_for = [
        "image editing / inpainting (Image-01 is text-to-image only)",
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the image to generate.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "default": "1:1",
                "description": "Aspect ratio of the generated image.",
            },
            "n": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
                "maximum": 4,
                "description": "Number of images to generate.",
            },
            "seed": {
                "type": "integer",
                "minimum": 0,
                "description": "Random seed for reproducibility.",
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Where to write the image(s). Extension controls output "
                    "format: .png preserves RGBA, .jpg flattens to RGB, "
                    ".webp uses WebP."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2, retryable_errors=["rate_limit", "timeout"]
    )
    idempotency_key_fields = ["prompt", "aspect_ratio", "n", "seed"]
    side_effects = [
        "writes image file(s) to output_path",
        "calls MiniMax image_generation API",
    ]
    user_visible_verification = [
        "Inspect generated image for relevance, quality, and aspect ratio"
    ]

    ENDPOINT = "https://api.minimaxi.com/v1/image_generation"
    POLL_ENDPOINT_TEMPLATE = (
        "https://api.minimaxi.com/v1/image_generation/task/{task_id}"
    )
    POLL_INTERVAL_SECONDS = 2.0
    POLL_TIMEOUT_SECONDS = 60.0
    DEFAULT_MODEL = "image-01"
    COST_PER_IMAGE_USD = 0.003  # unverified estimate — see SKILL.md

    # ------------------------------------------------------------------
    # Status / cost
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        if os.environ.get("MINIMAX_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        n = int(inputs.get("n", 1))
        return n * self.COST_PER_IMAGE_USD

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            return ToolResult(
                success=False,
                error="MINIMAX_API_KEY not set. " + self.install_instructions,
            )

        start = time.time()
        try:
            payload = self._build_payload(inputs)
            response = requests.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()

            image_bytes_list = self._collect_image_bytes(data, api_key)
            if not image_bytes_list:
                return ToolResult(
                    success=False,
                    error=(
                        "Unrecognized MiniMax response shape (no images found): "
                        f"{self._truncate(data)}"
                    ),
                )

            ext = self._infer_extension(inputs.get("output_path"), image_bytes_list[0])
            output_paths = self._resolve_output_paths(
                inputs.get("output_path"), len(image_bytes_list), ext
            )
            for path, raw in zip(output_paths, image_bytes_list):
                path.parent.mkdir(parents=True, exist_ok=True)
                self._save_image(raw, path, ext)

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"MiniMax image generation failed: {self._safe_error(e)}",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "minimax",
                "model": self.DEFAULT_MODEL,
                "prompt": inputs["prompt"],
                "aspect_ratio": inputs.get("aspect_ratio", "1:1"),
                "output": str(output_paths[0]),
                "outputs": [str(p) for p in output_paths],
                "images_generated": len(output_paths),
                "cost_estimate_confidence": "low",
            },
            artifacts=[str(p) for p in output_paths],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=f"minimax/{self.DEFAULT_MODEL}",
        )

    # ------------------------------------------------------------------
    # Payload + response parsing
    # ------------------------------------------------------------------

    def _build_payload(self, inputs: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.DEFAULT_MODEL,
            "prompt": inputs["prompt"],
            "aspect_ratio": inputs.get("aspect_ratio", "1:1"),
            "n": int(inputs.get("n", 1)),
        }
        if inputs.get("seed") is not None:
            body["seed"] = int(inputs["seed"])
        return body

    def _collect_image_bytes(
        self, data: dict[str, Any], api_key: str
    ) -> list[bytes]:
        """Return raw image bytes for every generated image.

        Tries sync shapes FIRST (because the real MiniMax API returns both
        an `id` and the images inline — treating `id` as a poll-target would
        trigger useless polling on a sync response):

          1. sync `images[].url` (flat list) → GET each URL
          2. sync `data.image_urls[]` (nested under `data`) → GET each URL
             [confirmed real MiniMax response shape — observed 2026-08]
          3. sync `data[].b64_json` → decode each entry
          4. ONLY if no images found: async `task_id` → poll status URL

        Returns [] on unrecognized shapes (caller turns this into ToolResult error).
        """
        # 1) Flat URL list: images[].url
        urls: list[str] = []
        for img in data.get("images") or []:
            url = img.get("url") if isinstance(img, dict) else None
            if url:
                urls.append(url)

        # 2) Nested URL list: data.image_urls[] — actual MiniMax shape
        if not urls and isinstance(data.get("data"), dict):
            for url in data["data"].get("image_urls") or []:
                if isinstance(url, str):
                    urls.append(url)

        if urls:
            out: list[bytes] = []
            for url in urls:
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                out.append(r.content)
            return out

        # 3) Sync base64 inline
        b64_items: list[str] = []
        for item in data.get("data") or []:
            if isinstance(item, dict):
                b64 = item.get("b64_json")
                if b64:
                    b64_items.append(b64)
        if b64_items:
            return [base64.b64decode(b) for b in b64_items]

        # 4) Only now fall back to async polling — and only if the response
        #    looks async (task_id explicit, OR an id with no images at all).
        task_id = data.get("task_id") or data.get("id")
        if task_id:
            polled = self._poll_task(task_id, api_key)
            if polled is not None:
                return self._collect_image_bytes(polled, api_key)
            return []

        return []

    def _poll_task(
        self, task_id: str, api_key: str
    ) -> dict[str, Any] | None:
        """Poll the async task endpoint until done or timeout.

        Returns the final response dict (containing images/task_id) on success,
        or None if polling timed out / errored.
        """
        url = self.POLL_ENDPOINT_TEMPLATE.format(task_id=task_id)
        deadline = time.time() + self.POLL_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                r = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30,
                )
                r.raise_for_status()
                polled = r.json()
            except Exception:
                time.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            status = (polled.get("status") or "").lower()
            if status in {"succeeded", "success", "completed", "done"}:
                return polled
            if status in {"failed", "error", "cancelled"}:
                return None
            time.sleep(self.POLL_INTERVAL_SECONDS)
        return None

    # ------------------------------------------------------------------
    # Output path + image save helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_output_paths(
        output_path: str | None, count: int, extension: str
    ) -> list[Path]:
        """One path per image. Single image keeps the base path; multiple
        get `_1`, `_2`, … suffixes (mirrors dashscope_image / openai_image)."""
        ext = extension if extension.startswith(".") else f".{extension}"
        if not output_path:
            return [Path(f"generated_image_{i + 1}{ext}") for i in range(count)]

        path = Path(output_path)
        suffix = path.suffix or ext
        if count == 1:
            return [path if path.suffix else path.with_suffix(suffix)]

        base = path.with_suffix("") if path.suffix else path
        return [base.parent / f"{base.name}_{i + 1}{suffix}" for i in range(count)]

    @staticmethod
    def _infer_extension(output_path: str | None, first_image: bytes) -> str:
        """Decide output extension: explicit in output_path wins, else
        sniff from the first image bytes (PNG vs JPEG vs WebP magic)."""
        if output_path:
            suf = Path(output_path).suffix.lower()
            if suf in {".png", ".jpg", ".jpeg", ".webp"}:
                return "jpg" if suf == ".jpeg" else suf.lstrip(".")
        if first_image.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if first_image.startswith(b"RIFF") and first_image[8:12] == b"WEBP":
            return "webp"
        if first_image.startswith(b"\xff\xd8\xff"):
            return "jpg"
        return "png"

    @staticmethod
    def _save_image(raw: bytes, path: Path, ext: str) -> None:
        """Decode with PIL, normalize mode for the requested format, save.

        Pillow can't save RGBA pixels into JPEG — flatten onto white first.
        This is the one place we actually use the new Pillow install.

        PIL is imported lazily so registry discovery doesn't pay the import
        cost on every tool list.
        """
        from PIL import Image as _PILImage

        img = _PILImage.open(io.BytesIO(raw))
        img.load()  # force decode now so we fail fast on corrupt bytes

        if ext in {"jpg", "jpeg"} and img.mode in {"RGBA", "LA", "P"}:
            background = _PILImage.new("RGB", img.size, (255, 255, 255))
            img_rgba = img.convert("RGBA") if img.mode != "RGBA" else img
            background.paste(img_rgba, mask=img_rgba.split()[-1])
            img = background
        elif img.mode == "P":
            img = img.convert("RGBA")

        save_kwargs: dict[str, Any] = {}
        if ext in {"jpg", "jpeg"}:
            img = img.convert("RGB")
            save_kwargs["quality"] = 92
            save_kwargs["optimize"] = True
        elif ext == "webp":
            save_kwargs["quality"] = 90
            save_kwargs["method"] = 6

        img.save(path, **save_kwargs)

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Strip the API key out of error messages before returning them."""
        key = os.environ.get("MINIMAX_API_KEY", "")
        if not key:
            return str(exc)
        return str(exc).replace(key, "[redacted]")

    @staticmethod
    def _truncate(data: Any, limit: int = 200) -> str:
        """Short, JSON-shaped representation of an unknown response."""
        import json
        try:
            text = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(data)
        return text if len(text) <= limit else text[:limit] + "..."
