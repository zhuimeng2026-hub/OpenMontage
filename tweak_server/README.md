# OpenMontage Tweak Server

A lightweight sidecar FastAPI app that lets end users tweak the rendering
script (Remotion props) of an existing OpenMontage project through a browser
form, then dispatches the render through the local MCP server.

**Architecture**: see `/root/.claude/plans/rosy-dazzling-bear.md`.

## What it does

- Reads the current props template from a project's directory (or falls back
  to `remotion-composer/public/sample-props/the-refactor-serenade-sample.json`)
- Renders a vanilla-JS form (`/projects/<id>/tweak`) where users can change:
  - **theme** (5 whitelisted playbooks)
  - per-cut: `text`, `fontSize`, `color`, `backgroundColor` (text cards)
  - per-cut: `animation`, `backgroundColor` (image / video cuts)
  - per-cut: `in_seconds`, `out_seconds` (universal)
  - audio: `volume` (narration + music), `fadeIn/Out/offset` (music only)
- Submits → validates against a strict whitelist → merges into the template →
  dispatches `video_compose(operation="remotion_render", ...)` to the local
  MCP server over streamable-http JSON-RPC.
- Streams the result back to the browser (rendering is **synchronous** at the
  HTTP level — expect 30-90s for a 60s video).
- Appends a `decision_log_tweak_revNNN.json` to the project dir (one entry per
  submission, append-only per `CLAUDE.md` invariant 6).
- Snapshots the full merged props next to each log entry for replay.

## What it does NOT do

- Does **not** modify the MCP server, the Remotion composer source, or
  openclaw-gateway.
- Does **not** expose asset upload / new image generation / chart data
  editing — those need developer intervention (see plan §10).
- Does **not** use `create_remotion_video_share` — that high-level workflow
  needs the full `asset_manifest` + `proposal_packet` + `scene_plan` triple,
  overkill for one-tweak-one-render UX. We call `video_compose` directly.

## Quick start

```bash
# 1. Make sure MCP server is running on :8900 (it usually is — check with
#    `curl http://127.0.0.1:8900/` — should be HTTP 401 if Bearer auth on).
cd /opt/OpenMontage_Voicebox

# 2. Set the MCP API token (read from .env — keep it server-side; never
#    send it to the browser):
export MCP_API_TOKEN=$(grep ^MCP_API_TOKEN= .env | cut -d= -f2-)

# 3. Optional: set the tweak server's own auth bearer (clients send it as
#    `X-Tweak-Token` header). If empty, auth is disabled (development only).
export TWEAK_SERVER_BEARER="local-dev-tweak-token-2026"

# 4. Start
make tweak-server
# or directly:
/opt/OpenMontage_Voicebox/.venv/bin/python -m uvicorn tweak_server.app:app \
  --host 0.0.0.0 --port 8901 --log-level info

# 5. Open the form
#    http://localhost:8901/projects/the-refactor-serenade/tweak
```

To stop: `make tweak-server-stop` (kills pid from `/tmp/tweak-server.pid`).

## API

| Method | Path                                       | Auth (`X-Tweak-Token`) | Purpose                          |
|--------|--------------------------------------------|------------------------|----------------------------------|
| GET    | `/`                                        | no                     | health + capability snapshot     |
| GET    | `/projects/{project_id}/tweak`             | no                     | HTML form                        |
| GET    | `/api/projects/{project_id}`               | **yes**                | current props + schema metadata  |
| POST   | `/api/projects/{project_id}/tweak`         | **yes**                | submit tweak → render            |
| GET    | `/renders/{project_id}/{filename}.mp4`     | no                     | serve rendered preview           |
| GET    | `/static/{tweak.html, tweak.js, tweak.css}` | no                     | static assets                    |

### `POST /api/projects/{project_id}/tweak`

Request body (all fields optional):

```json
{
  "theme": "flat-motion-graphics",
  "cuts": [
    {
      "id": "punchline-text",
      "text": "Updated subtitle",
      "fontSize": 120,
      "color": "#FF6B6B",
      "backgroundColor": "#0F172A"
    }
  ],
  "audio": {
    "music": {"volume": 0.25, "fadeInSeconds": 1.0}
  },
  "comment": "What did you change and why?"
}
```

Response (200 / 502):

```json
{
  "success": true,
  "project_id": "the-refactor-serenade",
  "staging_id": "tweak-c8596aaaf030",
  "output_path": ".../renders/tweak-20260828T114253Z.mp4",
  "duration_seconds": 139.12,
  "decision_log": "decision_log_tweak_rev006.json",
  "comment": "...",
  "merged_cuts_touched": ["punchline-text", "hook-terminal-desk"]
}
```

### Field whitelist (per `props_schema.py`)

| Field                          | Applies to                       | Range         |
|--------------------------------|----------------------------------|---------------|
| `theme`                        | top-level                        | 5 yaml names  |
| `cuts[].text`                  | text_card / hero_title / etc.    | ≤ 500 chars   |
| `cuts[].fontSize`              | text_card / hero_title           | 24 – 200      |
| `cuts[].color`                 | text_card / hero_title           | `#RRGGBB[AA]` |
| `cuts[].backgroundColor`       | any cut                          | `#RRGGBB[AA]` |
| `cuts[].animation`             | image / video (no `type`)        | enum          |
| `cuts[].in_seconds`/`out_seconds` | any cut                       | 0 – 600       |
| `audio.narration.volume`       | top-level                        | 0.0 – 1.0     |
| `audio.music.volume`           | top-level                        | 0.0 – 1.0     |
| `audio.music.fadeIn/Out`       | top-level                        | 0.0 – 3.0 s   |
| `audio.music.offsetSeconds`    | top-level                        | 0.0 – 30 s    |

**Forbidden** (rejected with 400): `id`, `source`, `type`, `*src` (any audio),
stat/chart fields. See `props_schema.py:_is_text_card` / `_is_image_or_video`.

## File layout

```
tweak_server/
├── __init__.py
├── app.py            FastAPI app + 5 routes + lifespan
├── mcp_client.py     JSON-RPC 2.0 over streamable-http (captures + echoes
│                     mcp-session-id, unwraps {content:[{text:...}]})
├── auth.py           X-Tweak-Token bearer check (constant-time compare)
├── props_schema.py   Pydantic models + field whitelist + merge_into_template
└── static/
    ├── tweak.html    Form (vanilla JS, no framework)
    ├── tweak.css     Minimal dark theme
    └── tweak.js      Form init + submit + result rendering
```

## Files touched in projects

Each successful tweak writes:

```
projects/<project-id>/decision_log_tweak_rev<NNN>.json          ← append-only log
projects/<project-id>/decision_log_tweak_rev_snapshot_<sid>.json ← full props
projects/<project-id>/renders/tweak-<ISO-timestamp>.mp4         ← output
```

## Env vars

| Name                      | Default                       | Purpose                          |
|---------------------------|-------------------------------|----------------------------------|
| `MCP_HTTP_URL`            | `http://127.0.0.1:8900`       | MCP server base URL              |
| `MCP_API_TOKEN`           | (none)                        | MCP Bearer token (if configured) |
| `TWEAK_SERVER_HOST`       | `127.0.0.1`                   | uvicorn bind                     |
| `TWEAK_SERVER_PORT`       | `8901`                        | uvicorn port                     |
| `TWEAK_SERVER_BEARER`     | (none, empty = auth off)      | clients send as `X-Tweak-Token`  |
| `TWEAK_RENDER_TIMEOUT_S`  | `600`                         | httpx read timeout (10 min)      |
| `TWEAK_SERVER_LOG_LEVEL`  | `INFO`                        | logging verbosity                 |

## Tests / verification

```bash
# Unit smoke tests (schema)
python -c "from tweak_server.props_schema import TweakPayload, merge_into_template; ..."

# Manual API smoke tests (already wired up):
TOKEN="local-dev-tweak-token-2026"
curl -H "X-Tweak-Token: $TOKEN" http://127.0.0.1:8901/api/projects/the-refactor-serenade | jq

curl -X POST -H "X-Tweak-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"cuts":[{"id":"punchline-text","fontSize":120,"color":"#FF6B6B"}],"comment":"test"}' \
  http://127.0.0.1:8901/api/projects/the-refactor-serenade/tweak
```

## Known limitations (see plan §10 for the full list)

- No progress streaming — user waits for the synchronous HTTP response
  (~2 minutes for a 60s video). Future: wire up `/render-progress/<job_id>`
  SSE bridge.
- No asset upload / new image generation — would need `asset_manifest` rebuild.
- No chart / stat data editing — needs per-component schema work.
- Decision-log entries are append-only; no UI for browsing history.
- No A/B comparison or rollback.