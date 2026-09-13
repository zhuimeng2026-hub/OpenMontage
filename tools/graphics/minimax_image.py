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
import logging
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# Two independent channels live here:
#
# 1. ``_log`` (DEBUG) — opt-in via ``OM_DEBUG_IMAGE_GEN=1``. Used when
#    reproducing a fault: dumps response shapes, poll status, save magic
#    bytes — every signal currently lost when ToolResult.error is the only
#    witness. Off by default so daily traffic stays clean.
#
# 2. ``_detail_log`` (INFO) — always on. Emits one structured event per
#    generation through the ``openmontage.gen_detail`` logger, written to
#    ``logs/gen_detail.log`` when running under mcp_server (configured in
#    mcp_server.py). Cheap enough for daily-use monitoring: tool / provider
#    / model / prompt_snippet / success / cost / duration.
#
# Both scrub API keys before logging.
_log = logging.getLogger(__name__)

_detail_log = logging.getLogger("openmontage.gen_detail")
# If the host (mcp_server.py) hasn't attached a handler, fall back to
# stderr so out-of-process scripts still produce output. Set
# OM_GEN_DETAIL_QUIET=1 to suppress the fallback (e.g. in noisy tests).
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

if os.environ.get("OM_DEBUG_IMAGE_GEN") == "1":
    _log.setLevel(logging.DEBUG)
    # Attach a stderr handler so OM_DEBUG is visible even when the host
    # (mcp_server.py / a script) hasn't configured root. Keep propagate=True
    # so an externally configured FileHandler also receives these lines.
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
    """Truncate a prompt for log lines without leaking secrets."""
    if not prompt:
        return ""
    s = prompt.replace("\n", " ").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _data_keys(data: Any) -> list[str]:
    """Return top-level dict keys + nested `data` keys, redacted."""
    if not isinstance(data, dict):
        return [type(data).__name__]
    keys = list(data.keys())
    nested = data.get("data")
    if isinstance(nested, dict):
        keys.append("data." + "/".join(nested.keys()))
    return [str(k) for k in keys]


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
        prompt_snippet = _prompt_snippet(inputs.get("prompt", ""))
        _log.debug(
            "minimax_image.execute enter prompt=%r aspect=%s n=%s seed=%s output=%s",
            prompt_snippet,
            inputs.get("aspect_ratio"),
            inputs.get("n"),
            inputs.get("seed"),
            inputs.get("output_path"),
        )
        _detail_log.info(
            "event=image_gen_detail state=submit tool=minimax_image provider=%s model=%s "
            "aspect=%s n=%s prompt=%r scene_id=%s",
            self.provider,
            self.DEFAULT_MODEL,
            inputs.get("aspect_ratio"),
            inputs.get("n"),
            prompt_snippet,
            inputs.get("scene_id"),
        )
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
            _log.debug(
                "minimax_image.execute http status=%s elapsed_ms=%d",
                response.status_code,
                round((time.time() - start) * 1000),
            )
            response.raise_for_status()
            data = response.json()
            _log.debug(
                "minimax_image.execute response keys=%s", _data_keys(data)
            )

            image_bytes_list = self._collect_image_bytes(data, api_key)
            # NEW: also extract the source URLs so the agent can reuse them
            # without re-uploading (e.g. as first_frame_image for I2V).
            image_urls = self._extract_image_urls(data)
            if not image_bytes_list:
                err = (
                    "Unrecognized MiniMax response shape (no images found): "
                    f"{self._truncate(data)}"
                )
                _log.warning(
                    "minimax_image.execute no_images keys=%s truncated=%r",
                    _data_keys(data),
                    _redact(self._truncate(data)),
                )
                _detail_log.info(
                    "event=image_gen_detail state=done tool=minimax_image success=false "
                    "reason=unrecognized_response keys=%s duration_s=%.2f",
                    _data_keys(data),
                    time.time() - start,
                )
                return ToolResult(success=False, error=err)

            ext = self._infer_extension(inputs.get("output_path"), image_bytes_list[0])
            output_paths = self._resolve_output_paths(
                inputs.get("output_path"), len(image_bytes_list), ext
            )
            for path, raw in zip(output_paths, image_bytes_list):
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._save_image(raw, path, ext)
                except Exception as save_err:
                    _log.error(
                        "minimax_image.execute save_failed path=%s bytes=%d magic=%s error=%s",
                        path,
                        len(raw),
                        raw[:16].hex(),
                        _redact(str(save_err)),
                    )
                    raise

        except Exception as e:
            err = f"MiniMax image generation failed: {self._safe_error(e)}"
            _log.error("minimax_image.execute error=%s", _redact(str(e)))
            _detail_log.info(
                "event=image_gen_detail state=done tool=minimax_image success=false "
                "reason=exception error_class=%s duration_s=%.2f",
                type(e).__name__,
                time.time() - start,
            )
            return ToolResult(success=False, error=err)

        duration_s = round(time.time() - start, 2)
        _log.debug(
            "minimax_image.execute ok outputs=%d bytes_total=%d duration_s=%.2f",
            len(output_paths),
            sum(p.stat().st_size for p in output_paths if p.exists()),
            duration_s,
        )
        _detail_log.info(
            "event=image_gen_detail state=done tool=minimax_image success=true "
            "outputs=%d cost_usd=%.4f duration_s=%.2f",
            len(output_paths),
            self.estimate_cost(inputs),
            duration_s,
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
                # NEW: surface the OSS signed URLs from the API response so
                # downstream tools (e.g. minimax_video_direct I2V first_frame_image)
                # can reuse them without uploading the local file again.
                "image_urls": image_urls,
                "cost_estimate_confidence": "low",
            },
            artifacts=[str(p) for p in output_paths],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=duration_s,
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

    def _extract_image_urls(self, data: dict[str, Any]) -> list[str]:
        """Pull source HTTPS URLs out of a MiniMax image response without downloading.

        Useful when the agent wants to forward the URL to a downstream consumer
        (e.g. minimax_video_direct's `first_frame_image` parameter) without
        re-uploading the local file copy. Recognizes the same two sync shapes
        that `_collect_image_bytes` does:
          1. flat `images[].url`
          2. nested `data.image_urls[]` (confirmed live MiniMax shape)

        Returns [] for base64-only or async (task_id) responses — no URL
        to surface in those shapes.
        """
        urls: list[str] = []
        for img in data.get("images") or []:
            url = img.get("url") if isinstance(img, dict) else None
            if url:
                urls.append(url)
        if urls:
            return urls
        if isinstance(data.get("data"), dict):
            for url in data["data"].get("image_urls") or []:
                if isinstance(url, str):
                    urls.append(url)
        return urls

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
        if urls:
            _log.debug(
                "_collect_image_bytes branch=flat_urls count=%d keys=%s",
                len(urls),
                _data_keys(data),
            )
            out: list[bytes] = []
            for url in urls:
                r = requests.get(url, timeout=60)
                _log.debug(
                    "_collect_image_bytes download url=%s status=%s bytes=%d",
                    _redact(url),
                    r.status_code,
                    len(r.content),
                )
                r.raise_for_status()
                out.append(r.content)
            return out

        # 2) Nested URL list: data.image_urls[] — actual MiniMax shape
        if isinstance(data.get("data"), dict):
            for url in data["data"].get("image_urls") or []:
                if isinstance(url, str):
                    urls.append(url)
            if urls:
                _log.debug(
                    "_collect_image_bytes branch=nested_urls count=%d keys=%s",
                    len(urls),
                    _data_keys(data),
                )
                out = []
                for url in urls:
                    r = requests.get(url, timeout=60)
                    _log.debug(
                        "_collect_image_bytes download url=%s status=%s bytes=%d",
                        _redact(url),
                        r.status_code,
                        len(r.content),
                    )
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
            _log.debug(
                "_collect_image_bytes branch=b64_inline count=%d", len(b64_items)
            )
            return [base64.b64decode(b) for b in b64_items]

        # 4) Only now fall back to async polling — and only if the response
        #    looks async (task_id explicit, OR an id with no images at all).
        task_id = data.get("task_id") or data.get("id")
        if task_id:
            _log.debug(
                "_collect_image_bytes branch=async_poll task_id=%s keys=%s",
                task_id,
                _data_keys(data),
            )
            polled = self._poll_task(task_id, api_key)
            if polled is not None:
                return self._collect_image_bytes(polled, api_key)
            return []

        _log.debug(
            "_collect_image_bytes branch=none keys=%s", _data_keys(data)
        )
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
        poll_idx = 0
        while time.time() < deadline:
            poll_idx += 1
            try:
                r = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30,
                )
                r.raise_for_status()
                polled = r.json()
            except Exception as poll_err:
                _log.debug(
                    "_poll_task poll=%d transient_error=%s elapsed_s=%.1f",
                    poll_idx,
                    type(poll_err).__name__,
                    self.POLL_TIMEOUT_SECONDS - (deadline - time.time()),
                )
                time.sleep(self.POLL_INTERVAL_SECONDS)
                continue

            status = (polled.get("status") or "").lower()
            _log.debug(
                "_poll_task poll=%d status=%s elapsed_s=%.1f keys=%s",
                poll_idx,
                status,
                self.POLL_TIMEOUT_SECONDS - (deadline - time.time()),
                _data_keys(polled),
            )
            if status in {"succeeded", "success", "completed", "done"}:
                return polled
            if status in {"failed", "error", "cancelled"}:
                _log.warning(
                    "_poll_task terminal_status=%s payload=%r",
                    status,
                    _redact(self._truncate(polled)),
                )
                return None
            time.sleep(self.POLL_INTERVAL_SECONDS)
        _log.warning(
            "_poll_task timeout task_id=%s polls=%d timeout_s=%.0f",
            task_id,
            poll_idx,
            self.POLL_TIMEOUT_SECONDS,
        )
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
