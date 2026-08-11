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
   every image path, renders a Remotion MP4, uploads it to Weiyun, creates a
   share link, and returns the link with a customer-facing message.
5. A successful batch becomes `published`. The next image upload in the same
   MCP session starts a new batch.

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

Success response:

```json
{
  "success": true,
  "status": "published",
  "asset_count": 3,
  "message": "视频已生成，点击下面的微云链接查看。",
  "share_url": "https://share.weiyun.com/...",
  "video_path": "projects/.../renders/...mp4",
  "duration_seconds": 9,
  "batch_id": "..."
}
```

Failures identify the failing stage (`session`, `render`, `weiyun_upload`, or
`weiyun_share`) and retain `video_path` after a publish failure.

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

Publishing uses tracked code only:

1. `weiyun_upload` uploads the MP4 and returns `file_id`.
2. `weiyun.gen_share_link` creates the customer-facing `short_url`.

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
