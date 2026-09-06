# MCP Decompose-and-Recompose

> Decompose a source video into scenes / transcript / keyframes via the
> OpenMontage MCP server, upload your own elements (image, narration, subtitle),
> and re-compose a new video — all through MCP tool calls. End-to-end:
> the server never writes outside `projects/<project-id>/`.

## When to Use This Skill

- A user wants to **remix / re-edit an existing video** without losing the
  source footage's motion / pacing.
- The source has no narration but you need to add your own (e.g. Chinese
  voice-over on an English motion-graphics reel).
- You want to add a title card, logo, watermark, or callout over the source.
- You're proving out the MCP server end-to-end: every call is a real JSON-RPC
  request to `http://<host>:8900/mcp`, no ad-hoc Python orchestrator.

**Not for:** from-scratch generation (use `animated-explainer` /
`cinematic` pipelines instead). This skill is the "**take footage, mutate it,
deliver**" lane.

## Quick Reference Card

```
PHASE 1 — DECOMPOSE:   scene_detect + transcriber + video_analyzer
PHASE 2 — STAGE OWN:   upload_asset (image / audio / subtitle)
PHASE 3 — RECOMPOSE:   video_compose (operation=overlay) + audio mix
PHASE 4 — VERIFY:      ffprobe on output, extract proof frame
```

| Step | MCP tool | Inputs you provide |
|---|---|---|
| 1.1 | `scene_detect` | `input_path`, `method=content`, `threshold=0.1-0.3`, `min_scene_length_seconds=1.5`, `output_path` |
| 1.2 | `transcriber` | `input_path`, `model_size=<absolute snapshot path>`, `language` |
| 1.3 | `video_analyzer` | `source`, `analysis_depth=standard`, `max_keyframes=8`, `output_dir` |
| 2.1 | `upload_asset` (×N) | `project_id`, `filename`, `content_base64`, `mime_type` |
| 2.2 | `edge_tts` | `text`, `voice=zh-CN-XiaoxiaoNeural`, `output_path` |
| 3.1 | `video_compose` | `operation=overlay`, `input_path`, `output_path`, `overlays[]`, `audio_path`, `codec`, `crf`, `preset` |

## MCP Session Lifecycle

Every call after `initialize` requires the `mcp-session-id` header. The
flow is **initialize → remember session → reuse on every subsequent call**:

```bash
TOKEN=$(grep '^MCP_API_TOKEN=' .env | cut -d= -f2-)
SESSION=$(curl -si -X POST http://localhost:8900/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-03-26",
                 "capabilities":{},
                 "clientInfo":{"name":"my-agent","version":"1"}}}' \
  | tr -d '\r' | awk -F': ' '/^mcp-session-id:/{print $2; exit}')
echo "${SESSION}" > /tmp/mcp_session.txt
```

Reuse `Authorization: Bearer ${TOKEN}` + `mcp-session-id: ${SESSION}` on every
follow-up `tools/call`. Any `tools/list` without a session after the first
call returns `400 Bad Request: Missing session ID`.

The full Python helper is at
[`scripts/mcp_helper.py`](../../scripts/mcp_helper.py) (built into the repo).
It wraps the boilerplate so an agent only writes the JSON-RPC method + params.

## The Four Phases (Concretely)

### Phase 1 — Decompose

Three MCP calls. Each returns a JSON artifact written under
`projects/<project-id>/artifacts/`.

```bash
# 1.1 Scene detection (FFmpeg content-based, threshold governs cut sensitivity)
cat > scene_inputs.json <<EOF
{"input_path":"assets/source.mp4","method":"content","threshold":0.3,
 "min_scene_length_seconds":2.0,
 "output_path":"projects/${PID}/artifacts/scenes.json"}
EOF
python3 scripts/mcp_helper.py exec scene_detect scene_inputs.json

# 1.2 Transcription (faster-whisper, local snapshot path — see Gotcha #1)
SNAP=/root/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66
cat > tx_inputs.json <<EOF
{"input_path":"assets/source.mp4","model_size":"${SNAP}",
 "language":"en","diarize":false,
 "output_dir":"projects/${PID}/artifacts"}
EOF
python3 scripts/mcp_helper.py exec transcriber tx_inputs.json

# 1.3 Comprehensive analysis (style profile + keyframes + suggested pipeline)
cat > va_inputs.json <<EOF
{"source":"assets/source.mp4","analysis_depth":"standard","max_keyframes":8,
 "output_dir":"projects/${PID}/artifacts"}
EOF
python3 scripts/mcp_helper.py exec video_analyzer va_inputs.json
```

Outputs to inspect:
- `artifacts/scenes.json` — `[{index, start_seconds, end_seconds, ...}]`
- `artifacts/<basename>_transcript.json` — `segments[]`, `word_timestamps[]`
- `artifacts/video_analysis_brief.json` — `summary`, `style_profile`,
  `replication_guidance.suggested_pipeline`,
  `replication_guidance.motion_required`
- `artifacts/keyframes/frame_*.jpg` — extracted keyframes

### Phase 2 — Add Own Elements

Two paths depending on where the asset is born:

**Born on the server** (generated locally):
```bash
# 2.1 Generate Chinese narration via MCP edge_tts
python3 scripts/mcp_helper.py exec edge_tts tts_inputs.json
```

**Born on the client** (image, voice memo, custom asset the user uploaded):
```bash
# 2.2 Upload via MCP — server writes to projects/<pid>/assets/_sessions/<hash>/
python3 scripts/upload_assets.py
# After upload, copy / symlink from _sessions/<hash>/ into projects/<pid>/assets/
# so video_compose can reference the canonical path.
```

> **Session isolation is intentional.** Uploaded assets land under
> `assets/_sessions/<session_hash>/` to keep one user's batch from leaking
> into another's project. After the upload succeeds, stage the canonical
> paths yourself before invoking `video_compose`.

### Phase 3 — Recompose

Use `video_compose` operation=`overlay` for FFmpeg-based image burn-in + audio
mix. This path is always available (no Node/Remotion/HyperFrames dependency).

```bash
cat > compose_inputs.json <<EOF
{
  "operation": "overlay",
  "input_path": "assets/source.mp4",
  "output_path": "projects/${PID}/renders/final.mp4",
  "audio_path": "projects/${PID}/assets/audio/zh_narration.mp3",
  "overlays": [{
    "asset_path": "projects/${PID}/assets/images/zh_title_card.jpg",
    "start_seconds": 0.0,
    "end_seconds": 3.0,
    "x": 0, "y": 0, "scale": 1.0,
    "fade_in": true, "fade_out": true
  }],
  "options": {"audio_volume": 0.6, "audio_delay_seconds": 0.5},
  "codec": "libx264", "crf": 22, "preset": "fast"
}
EOF
python3 scripts/mcp_helper.py exec video_compose compose_inputs.json
```

> **Overlay schema gotcha:** the field is `asset_path`, NOT `path`. The doc
> example uses `path`, but the implementation reads `ov["asset_path"]` and
> raises `KeyError: 'asset_path'` if missing. Always use `asset_path`.

### Phase 4 — Verify

The proof frame at a known timestamp must show your overlay:
```bash
ffmpeg -v error -ss 1.5 -i projects/${PID}/renders/final.mp4 \
  -frames:v 1 -y /tmp/proof_title.png
```

For deeper validation, ffprobe duration / codec / bitrate match the source
within a few percent (overlay path keeps source bitrate, just adds burn-in).

## Gotchas (Learned the Hard Way)

### 1. Whisper model resolution requires an absolute snapshot path

`tools/analysis/transcriber.py` resolves the model BEFORE instantiation and
**never fetches from huggingface.co**. The bare size alias `"base"` works in
theory but on this host the resolution can fail with
`LocalEntryNotFoundError` if the resolver's expectations don't match the
on-disk layout. Bullet-proof call:

```python
SNAP = ("/root/.cache/huggingface/hub/models--Systran--faster-whisper-base/"
        "snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66")
inputs = {"input_path": src, "model_size": SNAP, "language": "en",
          "output_dir": out_dir}
```

`turbo` alias maps to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` —
that's NOT the cached `openai/whisper-large-v3-turbo` snapshot, which is in
the original Whisper safetensors format and won't load via faster-whisper.
If you need turbo, pass the absolute path to a CTranslate2-format snapshot.

### 2. MCP session ID is required after initialize

`streamable-http` transport gives you `mcp-session-id` in the response header
of the first `initialize` call. Every subsequent `tools/call` MUST carry it
or the server returns `400 Bad Request: Missing session ID`. Reuse one
session for the whole workflow.

### 3. Upload assets land in `_sessions/<hash>/`

`upload_asset` writes to `projects/<pid>/assets/_sessions/<session_hash>/`
to keep batches isolated by `Mcp-Session-Id`. Before calling `video_compose`,
copy or symlink the uploaded files into `projects/<pid>/assets/images/` or
`projects/<pid>/assets/audio/` so the canonical paths line up with what
`video_compose` expects.

### 4. Scene detect may return 1 scene even with low threshold

If the source is a continuous motion-graphics piece with no hard cuts, even
`threshold=0.1` will collapse to one scene. That is correct — don't tune it
further. Use the keyframes + style profile from `video_analyzer` instead.

### 5. overlay() default schema field is `asset_path`

Not `path`, not `file`, not `image`. See `tools/video/video_compose.py:2802`.

### 6. Per-session batch lock — re-init MCP session per project

The server enforces a per-session upload batch: once a session has an open
`upload_asset` batch for one project id, subsequent `upload_asset` calls to a
**different** project from the same session fail with:

> `MCP session is already collecting assets for another project`

Solution: re-initialize the MCP session whenever the project id changes. The
helper `scripts/mcp_decompose_and_recompose.py` does this automatically by
caching sessions in `/tmp/mcp_session.txt.<project_id>`. If you call the MCP
manually, run `python3 scripts/mcp_helper.py init` to get a fresh session id
between projects.

## Worked Example

The full input / output of a real run that this skill was validated against:

| Step | Tool | Outcome |
|---|---|---|
| init | `lib.checkpoint.init_project("mcp-decompose-demo-...","MCP Decompose-and-Recompose Demo","hybrid")` | workspace created |
| 1.1 | `scene_detect` on `assets/signal-from-tomorrow-demo.mp4` | 1 scene, 0.0–30.059s |
| 1.2 | `transcriber` w/ base snapshot path | 0 segments (silent source) |
| 1.3 | `video_analyzer` standard depth | keyframes×2, `motion_required=true`, suggested `cinematic / flat-motion-graphics` |
| 2.1 | `edge_tts` zh-CN-XiaoxiaoNeural | 57-char narration → `zh_narration.mp3` (63.5 KB) |
| 2.2 | `upload_asset` × 2 (image + audio) | session_assets list returns both |
| 3.1 | `video_compose` `operation=overlay` | `renders/final.mp4` 1920×1080 H.264+AAC 30s (9.45 MB) in 12.22s wall |
| 4.1 | `ffmpeg -ss 1.5 -frames:v 1` | title card frame extracted, visually verified |

## Quick Run (One-Shot)

```bash
python3 scripts/mcp_decompose_and_recompose.py \
  --project my-demo-$(date +%s) \
  --source path/to/source.mp4 \
  --title "《我的标题》" \
  --narration "一段旁白..." \
  --overlay-start 0 --overlay-end 3
```

Defaults to `assets/signal-from-tomorrow-demo.mp4` if `--source` is omitted.
Produces `projects/<project-id>/renders/final.mp4` + extracts `/tmp/mcp_decompose_proof.png`.

The orchestrator re-initializes the MCP session per project automatically
(see Gotcha #6).

## Files This Skill Touches

- `projects/<project-id>/artifacts/` — analysis JSON + keyframes
- `projects/<project-id>/assets/images/` — staged images for compose
- `projects/<project-id>/assets/audio/` — staged audio for compose
- `projects/<project-id>/assets/_sessions/<hash>/` — raw MCP upload staging
- `projects/<project-id>/renders/final.mp4` — the final deliverable
- `scripts/mcp_helper.py` — JSON-RPC helper used by every phase
- `scripts/mcp_upload.py` — base64 uploader used by Phase 2
- `scripts/mcp_decompose_and_recompose.py` — one-shot orchestrator

## Related Skills

- `skills/creative/video-understand-usage.md` — local CLIP/BLIP2/LLaVA
  alternatives when MCP analysis is unavailable
- `skills/creative/scene-detect-usage.md` — pure-local scene detection
  (`pyscenedetect`, no MCP needed)
- `skills/core/subtitle-sync.md` — if the recompose adds subtitles
- `skills/creative/video-stitching.md` — multi-clip assembly beyond overlay