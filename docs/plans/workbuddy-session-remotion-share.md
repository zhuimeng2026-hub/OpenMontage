# WorkBuddy Session Photo Video + Weiyun Share

## Goal

Without changing WorkBuddy, let a customer upload one or more images through
the existing MCP connection, then send a natural-language instruction such as
"生成视频". OpenMontage must render a Remotion photo video from the images in
that MCP session, publish it to Weiyun, and return a customer-facing share URL.

The shared MCP API key authenticates the WorkBuddy client. `Mcp-Session-Id`
separates concurrent customer sessions; it is not a durable user identity.

## Ownership

- `OpenMontage-mcp-proxy`: authenticate, forward MCP headers and responses, and
  write transport-level correlation logs. It must not contain render or Weiyun
  business logic.
- `OpenMontage`: own session asset state, customer-facing tool messages,
  Remotion rendering, Weiyun publishing, and workflow logs.
- `WorkBuddy`: unchanged. It discovers MCP tool descriptions and forwards tool
  results to the customer.

## Customer flow

1. The customer uploads images through `upload_asset` or
   `upload_asset_chunk`.
2. Each completed image upload returns `status=collecting_assets`, the current
   `asset_count`, and a Chinese message telling the customer to continue
   uploading or send "生成视频".
3. When the customer asks to generate, WorkBuddy calls
   `create_remotion_video_share` based on its MCP tool description.
4. The tool loads the open batch for the current `Mcp-Session-Id`, validates
   every image path, claims a render job (`render_job_id`), and dispatches the
   render→upload→share pipeline to a **background thread**. It returns
   **immediately** with `status=queued` and the `render_job_id` — it does NOT
   block until the video is published. The client then polls
   `get_render_status(render_job_id)` to track progress and fetch the final
   `share_url` once `status=published`.
5. A successful batch reaches `status=published`. The next image upload in the
   same MCP session starts a new batch.

## MCP contracts

### Upload additions

Existing upload response fields remain compatible. Add:

```json
{
  "status": "collecting_assets",
  "asset_count": 2,
  "message": "已收到 2 张图片。你可以继续上传，上传完成后发送“生成视频”。",
  "next_action": "continue_upload_or_generate",
  "batch_id": "..."
}
```

Only completed image uploads increment the batch count. Video/audio uploads
retain existing behavior and are not included in the photo-video batch.

### `create_remotion_video_share`

The tool takes no image paths and no session identifier. Optional inputs may
include `project_id`, `duration_per_image`, `aspect_ratio`, and `title`.

Defaults:

- `duration_per_image`: 3 seconds
- `aspect_ratio`: `9:16`
- motion cycle: `zoom-in`, `pan-left`, `ken-burns`, `pan-right`
- renderer: explicitly locked to `remotion`; no silent FFmpeg fallback

This tool is **non-blocking**. It returns immediately after kicking off the
background pipeline; the `share_url` is NOT present in this response.

Immediate (queued) response:

```json
{
  "success": true,
  "status": "queued",
  "render_job_id": "<hex>",
  "batch_id": "...",
  "project_id": "...",
  "asset_count": 3,
  "duration_seconds": 9,
  "message": "视频渲染已在后台启动，请使用 get_render_status(render_job_id) 轮询进度与最终结果。"
}
```

To get the actual result, poll `get_render_status(render_job_id)` until
`status` reaches a terminal value (`published` or `failed`). On `published` the
response carries `share_url`; on `failed` it carries `error` and a `stage`
(failure stage) value.

`status` state machine: `queued` → `rendering` → (`rendered` → `uploading` →)
`published` (success) or `failed`. The `rendered` and `uploading` states are
transient intermediate progress markers.

Failures set `failure_stage` to one of `validation`, `render`,
`weiyun_upload`, `weiyun_share`, or `background_crash`, plus a human-readable
`error` string. When the failure happens at `weiyun_upload` or `weiyun_share`
the `video_path` is still retained, so the client can recover the partial
video. `validation` failures are written to session state and are reported by
`get_render_status` with `stage=validation`.

## Async client call sequence

The contract is request→poll, not request→result. A correct client flow:

1. **Upload images** via `upload_asset` / `upload_asset_chunk` until the batch
   is ready; each upload echoes `status=collecting_assets` and `batch_id`.
2. **Call `create_remotion_video_share`** (no paths/session args). It returns
   immediately:
   ```json
   {"success": true, "status": "queued", "render_job_id": "<hex>", "batch_id": "...", "message": "..."}
   ```
   Capture `render_job_id`. Do not expect `share_url` here.
3. **Poll `get_render_status(render_job_id)`** on an interval (e.g. every few
   seconds) until `status` is terminal:
   - `queued` → `rendering` → `rendered` → `uploading` → `published`: read
     `share_url` and hand it to the customer.
   - any state → `failed`: read `stage` (`failure_stage`) and `error`. If
     `video_path` is present, the partial video can still be retrieved.
4. **Retry / new batch**: `begin_render` clears the previous `error` and
   `failure_stage`, so re-calling `create_remotion_video_share` on the same
   session reports a clean state and does not carry stale failure markers.

```text
upload_asset ×N  →  create_remotion_video_share
                     ← {status:"queued", render_job_id:"..."}
loop:
  get_render_status(render_job_id)
  ← {status:"rendering"|"rendered"|"uploading", ...}   # keep polling
  ← {status:"published", share_url:"..."}              # done (success)
  ← {status:"failed", stage:"...", error:"..."}        # done (failure)
```

## Session state

Persist local JSON state below `projects/.mcp_sessions/`, keyed by a SHA-256
digest prefix of `Mcp-Session-Id`. Never use the raw session ID as a path.

Required state:

```json
{
  "project_id": "...",
  "batch_id": "...",
  "status": "collecting_assets",
  "assets": [],
  "created_at": "...",
  "updated_at": "...",
  "render_job_id": null,
  "video_path": null,
  "share_url": null
}
```

Writes must be atomic. A per-session lock prevents two concurrent generate
requests from rendering the same batch. Requests without a Streamable HTTP
session fail clearly instead of sharing a `legacy` namespace.

## Render and publish

Build one Remotion cut per image and an asset manifest that resolves each cut
to its session-owned local path. Write output under
`projects/<project_id>/renders/` with batch and job identifiers.

> The whole render→upload→share pipeline runs in a **background daemon
> thread** started by `create_remotion_video_share`. The MCP call returns
> before any of this executes; progress is observable only via
> `get_render_status(render_job_id)`.

Publishing uses tracked code only:

1. `weiyun_upload` uploads the MP4 and returns `file_id`.
2. `weiyun_share_link` (the token-based Weiyun share tool) creates the
   customer-facing `short_url` / `share_url`.

The implementation must not depend on untracked local files.

## Logging

Write rotating business logs to `logs/session_video.log`. Use JSON records when
practical. Required events:

- `asset_uploaded`
- `batch_collecting`
- `render_requested`
- `render_started`
- `render_completed`
- `weiyun_publish_started`
- `weiyun_publish_completed`
- `workflow_failed`

Correlation fields include `request_id`, `session_hash`, `project_id`,
`batch_id`, `asset_count`, `render_job_id`, `status`, and `duration_ms`.
Transport logs use only a short session hash. Logs must never contain Base64
media, API keys, cookies, tokens, or a full `Mcp-Session-Id`.

## Real-time progress (SSE)

Polling `get_render_status` only reflects coarse stages (`rendering` →
`rendered` → `uploading` → `published`). For live, frame-level progress, the
server exposes a Server-Sent Events stream:

```
GET /render-progress/{render_job_id}
Authorization: Bearer <MCP_API_TOKEN>
Accept: text/event-stream
```

Each `data:` frame is a JSON progress event:

```json
{
  "event": "render_progress",
  "render_job_id": "<hex>",
  "phase": "render|upload|share|snapshot|done",
  "status": "rendering|rendered|uploading|uploaded|sharing|published|failed",
  "percent": 42.0,
  "message": "Remotion rendering frame 126/300",
  "share_url": "https://share.weiyun.com/...",
  "error": "...",
  "ts": 1690000000.0
}
```

- The stream opens with a `snapshot` event carrying the current session state,
  so a client that connects mid-render immediately sees where things stand.
- During rendering, `phase=render` events carry Remotion's parsed frame
  percentage (`percent`); upload and share phases emit coarse `status` events.
- The stream sends `: keep-alive` heartbeats every ~1s while idle and closes
  when `status` reaches `published` or `failed`.
- The endpoint is mounted on the inner Starlette app, so it inherits the same
  Bearer-token auth as the MCP endpoint. Disable proxy buffering for it
  (`X-Accel-Buffering: no` is already set by the server; nginx needs
  `proxy_buffering off` for SSE).

## Verification

Tests mock Remotion and Weiyun network operations and cover:

- normal and chunked upload prompts/counts;
- cross-session isolation;
- a new batch after publication;
- single-image and multi-image render plans;
- duplicate-generation protection;
- render/upload/share failures;
- log redaction.

Deployment requires updating and restarting OpenMontage. The proxy only needs
redeployment if its session logging changes; standard reverse-proxy forwarding
already preserves `Mcp-Session-Id`.
