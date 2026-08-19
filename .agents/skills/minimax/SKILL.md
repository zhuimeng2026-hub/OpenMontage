---
name: minimax
description: MiniMax (Hailuo AI) direct API integration — image-01 text-to-image generation, plus reference for the existing fal.ai-gated MiniMax video path. Use when generating images with MiniMax Image-01, when the user mentions Hailuo or minimax-image, or when cost-effective Chinese-cloud image generation is preferred over OpenAI/FLUX.
---

# MiniMax (Hailuo AI)

> ⚠️ **Paid API.** The Image-01 endpoint bills per image. Test with small prompts (`n=1`, low-cost subjects) before committing to a full render run. Pricing is **unverified** in this skill — see the verification section below.

Requires `MINIMAX_API_KEY` in `.env`. Get one at https://intl.minimaxi.com/ (international) or https://api.minimaxi.com/ (China).

> **Note:** The video path (`tools/video/minimax_video.py`) currently uses `FAL_KEY` against the fal.ai gateway (`fal-ai/minimax/video-01`). That key still works for video. The new image tool in this skill uses `MINIMAX_API_KEY` against the **direct MiniMax REST endpoint** for cheaper pricing and no gateway dependency.

## Current API

### Image Generation (direct, Image-01)

```text
POST https://api.minimaxi.com/v1/image_generation
Header: Authorization: Bearer $MINIMAX_API_KEY
Header: Content-Type: application/json
```

- Model: `image-01` (only model known to this skill as of 2026-08)
- Body:
  ```json
  {
    "model": "image-01",
    "prompt": "a red apple on a white background",
    "aspect_ratio": "1:1",
    "n": 1,
    "seed": 12345
  }
  ```
- Response shape (observed live 2026-08 — confirmed working):
  - **Sync path (real shape):** `{ "id": "<uuid>", "data": { "image_urls": ["https://hailuo-image-algeng-data.oss-cn-wulanchabu.aliyuncs.com/..."] } }` — URLs nested under `data.image_urls`; tool downloads each.
  - **Alternative sync shapes (defensive):** flat `{ "images": [{ "url": ... }] }` and inline `{ "data": [{ "b64_json": ... }] }` are also accepted.
  - **Async path:** `{ "task_id": "..." }` → poll `GET https://api.minimaxi.com/v1/image_generation/task/{task_id}` until status is `succeeded` (or `failed`), then read images from the polled response. Sync shapes are checked FIRST because the real API returns both an `id` and the images inline — naively polling on `id` would loop until timeout.
- Aspect ratios accepted: `"1:1"`, `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"` (default `"1:1"`)
- `n`: 1-4 images per request

The tool (`tools/graphics/minimax_image.py`) handles **all three response shapes** (sync-URL, sync-base64, async-task-id). Any unrecognized shape returns a `ToolResult(success=False, error="Unrecognized response shape: ...")` so callers can diagnose.

### Video Generation (via fal.ai gateway — pre-existing)

The video path is **not** part of this skill's scope. See `tools/video/minimax_video.py` and the existing `FAL_KEY` setup. MiniMax video is accessible as `fal-ai/minimax/video-01` (Hailuo model family).

## OpenMontage Usage

### Via selector (preferred — auto-discovers all image providers)

```python
from tools.graphics.image_selector import ImageSelector

result = ImageSelector().execute({
    "preferred_provider": "minimax",
    "prompt": "a fluffy cat sitting on a windowsill at golden hour",
    "aspect_ratio": "16:9",
    "output_path": "projects/my-video/assets/images/cat.png",
})
```

### Direct tool call (force the MiniMax provider)

```python
from tools.graphics.minimax_image import MiniMaxImage

tool = MiniMaxImage()
print(tool.get_status())  # ToolStatus.AVAILABLE if MINIMAX_API_KEY is set

result = tool.execute({
    "prompt": "futuristic city skyline, neon lights, cyberpunk",
    "aspect_ratio": "16:9",
    "n": 1,
    "output_path": "projects/my-video/assets/images/skyline.png",
})

# result.data keys: provider, model, prompt, output, outputs, images_generated
# result.artifacts: list of written file paths
```

## Recommended Workflow

1. **Smoke test first.** Before committing, run one cheap generation (`n=1`, simple subject) to confirm `MINIMAX_API_KEY` is valid and the endpoint responds.
2. **Use the selector unless you need a specific provider.** `image_selector` ranks providers dynamically; explicit `preferred_provider="minimax"` overrides selection when needed.
3. **Cost tracking.** `result.cost_usd` is an estimate (see verification below) — record it in your project budget. `estimate_cost()` scales with `n`.
4. **PIL post-processing.** The tool uses Pillow to verify and normalize output (transparent RGBA → RGB for JPEG, transparent WebP handling). If you need explicit format conversion, save with a `.webp` or `.jpg` extension in `output_path` and the tool will re-encode.

## Parameters (`minimax_image`)

- `prompt` (required): text prompt for the image
- `aspect_ratio`: `"1:1"` (default), `"16:9"`, `"9:16"`, `"4:3"`, `"3:4"`
- `n`: number of images, 1-4, default 1
- `seed`: integer for reproducibility (optional)
- `output_path`: file path to write the image; extension controls format (`.png`, `.webp`, `.jpg`)

## Troubleshooting

- **401 Unauthorized:** Verify `MINIMAX_API_KEY` is set in `.env` and not the old typo `MNINIMAX_API_KEY`. The typo was renamed in 2026-08.
- **"Unrecognized response shape":** The MiniMax API returned JSON in a format the tool doesn't recognize. Update `_parse_response()` in `tools/graphics/minimax_image.py` to add the new branch.
- **RGBA → JPEG fails:** The tool handles this transparently (converts RGBA to RGB on white background before JPEG encode). If you see this error, file a bug — the auto-conversion should have caught it.
- **Polling timeout (async path):** Default polling waits up to 60s with 2s intervals. If your queue position is large, the task may need longer. (Future enhancement: make timeout configurable.)
- **Cost higher than expected:** `estimate_cost()` is unverified. Check your MiniMax billing console at https://intl.minimaxi.com/user-center/billing for actual rates.

## Verification (for this skill's maintainers)

- **Endpoint URL (`https://api.minimaxi.com/v1/image_generation`)**: ✅ **confirmed live** — actual response received 2026-08 with the shape documented above.
- **Model name (`image-01`)**: ✅ **confirmed live** — request to this model succeeded against the real API.
- **Sync response shape (`{id, data: {image_urls: [...]}}`)**: ✅ **confirmed live** — observed in production traffic; tool's `_collect_image_bytes` is ordered to check this shape FIRST (before async polling, because the same response carries an `id` field that naively looks like a poll target).
- **Async response shape (`{task_id: ...}` + poll)**: unverified live; the polling path is defensive code for a shape the API may use under load. The execute-path tests cover it with mocked responses.
- **Pricing (`estimate_cost()` returns ~$0.003 per image)**: still unverified — check your MiniMax billing console at https://intl.minimaxi.com/user-center/billing.
- **Rate limit (RPM)**: ⚠️ **observed live 2026-08**. A batch of 80 images at 4-way parallelism hit `{"base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"}}` mid-batch. The failed responses come back with `data: null` (not the usual `data: {image_urls: [...]}`), so the tool surfaces them via the standard "Unrecognized response shape" error. For batch generation, **stay at ≤3 concurrent workers**, and if you hit a 1002 error, **wait ~60s for the RPM window to reset before retrying**. The tool has no built-in rate-limit backoff — a retry loop is the caller's responsibility.

## Safety

Never print or write `MINIMAX_API_KEY` to logs, metadata, patches, or project artifacts. The tool's error messages are passed through unchanged from `requests` — if you see the key in an error message, redact it before sharing logs.
