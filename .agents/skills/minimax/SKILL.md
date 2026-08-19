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

### Video Generation — direct MiniMax REST API (preferred)

`tools/video/minimax_video_direct.py` is the canonical MiniMax video path. Bypasses the fal.ai gateway — billed by MiniMax directly, no gateway markup.

```text
POST https://api.minimaxi.com/v1/video_generation
Header: Authorization: Bearer $MINIMAX_API_KEY
Header: Content-Type: application/json
```

- Models known to this skill (confirmed live 2026-08):
  - `MiniMax-Hailuo-2.3` — current production model
  - `MiniMax-Hailuo-2.3-Fast` — cheaper, lower latency, same prompt grammar
- Body (text-to-video):
  ```json
  {
    "model": "MiniMax-Hailuo-2.3",
    "prompt": "A red apple on a white table, soft daylight, shallow depth of field",
    "duration": 6,
    "resolution": "768P",
    "prompt_optimizer": true
  }
  ```
- Submit response (sync, observed live): `{ "task_id": "...", "base_resp": { "status_code": 0, "status_msg": "success" } }`
  - **Do not re-poll on `task_id`** — submit is already synchronous; the `task_id` is the handle for the *next* step.
- Status polling: `GET https://api.minimaxi.com/v1/query/video_generation?task_id=<task_id>`
  - Status flow: `Preparing → Queueing → Processing → Success | Fail`
  - Success payload includes `file_id` and the final `video_width` / `video_height`.
- File retrieval: `GET https://api.minimaxi.com/v1/files/retrieve?file_id=<file_id>`
  - Returns `{ "download_url": "https://..." }`. Direct `GET /v1/files/{id}/content` returns **403**; only the retrieve-then-redirect pattern works.

#### Hard rules (verified live 2026-08)

- `resolution` MUST be uppercase `"768P"` or `"1080P"`. Lowercase `"768p"` returns `base_resp.status_code=2013 ("invalid params, model ... does not support resolution 768p")`.
- `duration` must be one of `[6, 10]` (seconds).
- If `base_resp.status_code != 0` on submit, **do not enter the poll loop** — the request was rejected and there is nothing to wait for.
- The download URL from `/v1/files/retrieve` is short-lived (typically minutes). Retrieve → GET immediately; don't stage.

#### OpenMontage Usage

```python
from tools.video.minimax_video_direct import MiniMaxVideoDirect

tool = MiniMaxVideoDirect()
print(tool.get_status())  # ToolStatus.AVAILABLE if MINIMAX_API_KEY is set

result = tool.execute({
    "prompt": "A stack of old books on a wooden desk, slow cinematic dolly-in",
    "model": "MiniMax-Hailuo-2.3",
    "duration": 6,
    "resolution": "768P",
    "prompt_optimizer": True,
    "output_path": "projects/my-video/assets/video/clip.mp4",
    "poll_interval_seconds": 5,
    "timeout_seconds": 600,
})
# result.data keys: provider, model, operation, prompt, duration, resolution,
#                   task_id, file_id, output, bytes_written
# result.artifacts: list of written file paths
```

Via the video selector (preferred — auto-routes from prompt):

```python
from tools.video.video_selector import VideoSelector

result = VideoSelector().execute({
    "preferred_provider": "minimax_direct",
    "prompt": "...",
    "output_path": "projects/my-video/assets/video/clip.mp4",
})
```

#### image_to_video (confirmed live 2026-08)

`MiniMax-Hailuo-2.3` accepts a `first_frame_image` field on the same `/v1/video_generation` endpoint. The full three-step flow (submit → poll → retrieve) works identically to text-to-video. Just attach `first_frame_image` to the payload; the tool auto-derives `operation: "image_to_video"`.

```python
result = tool.execute({
    "prompt": "the camera slowly dollies forward, golden light shifts across the brick",
    "first_frame_image": "https://...signed-oss-url.../ref.jpeg",
    "model": "MiniMax-Hailuo-2.3",
    "duration": 6,
    "resolution": "768P",
    "output_path": "projects/.../assets/video/clip_i2v.mp4",
})
```

**Why `first_frame_image` and not `image_url`?** The MiniMax API distinguishes T2V vs I2V by the **presence of the `first_frame_image` field on submit** — not by an `operation` flag. Without `first_frame_image` in the payload, the request is treated as T2V even when `image_url` is present. The legacy `image_url` field still works on the standard `MiniMax-Hailuo-2.3` model (it was accepted via the older validation path) but `MiniMax-Hailuo-2.3-Fast` rejects it with `does not support Text-to-Video mode`. The tool accepts both keys, but **always sends `first_frame_image` to the API**, and emits a deprecation warning if `image_url` was used.

**Requirements for `first_frame_image`:**
- Must be a publicly fetchable HTTPS URL (the model server downloads the image itself, not from the agent's filesystem).
- The MiniMax image tool (`minimax_image`) returns an Alibaba OSS signed URL (`hailuo-image-algeng-data.oss-cn-wulanchabu.aliyuncs.com/...`) that works out of the box — long-expiry signature, no extra auth needed.
- If you generate the image elsewhere (S3, fal.ai, etc.), use a signed URL or a CDN URL with the same accessibility profile.

**Known limits:**
- No multi-image / reference-set support on this endpoint (unlike Seedance 2.0).
- Output resolution is fixed by the request (`768P` / `1080P`); it does not adapt to the input image's aspect ratio. For 16:9 input, `768P` yields a 1366×768 clip. For other aspect ratios, the model letterboxes / pillarboxes.

### Video Generation (via fal.ai gateway — pre-existing, alternative)

`tools/video/minimax_video.py` (provider `minimax`, requires `FAL_KEY`) is the
older fal.ai-gated path. Use it when you don't have a MiniMax API key but
have a fal.ai subscription. MiniMax video is accessible there as
`fal-ai/minimax/video-01` (older model — only `video-01`; the Hailuo-2.3
family is **not** exposed on the fal.ai path as of 2026-08).

The direct path is preferred when `MINIMAX_API_KEY` is set — it's cheaper,
supports the current model family, and the agent has full control over
`model_variant`, `duration`, and `resolution`.

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
