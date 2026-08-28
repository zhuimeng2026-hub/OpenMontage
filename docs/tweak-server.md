# OpenMontage Tweak Server — Reference Doc

> **Status**: shipped (7 commits, base `b69b4a5` → `e6d59e0`). All features
> verified end-to-end. Do not re-implement without checking this doc first.

A lightweight sidecar FastAPI app at `:8901` that lets end users tweak the
rendering script (Remotion props) of an existing OpenMontage project through
a browser form, then dispatch the render through the local MCP server
(`:8900`) over JSON-RPC + stream progress back via SSE.

Related docs:

- `plans/remotion-studio-deployment.md` — **Studio** is for template authors
  (developer workstation with HMR). This tweak server is for **end users**
  (browser form, no HMR, async render + progress).
- `MCP_SERVER.md` — the upstream MCP server we proxy to.
- `MCP_SERVER.md#render-progress` — the SSE endpoint MCP exposes
  (we use it as a fallback data source, our local JobStore is authoritative).

## TL;DR

```bash
# Already set up via Makefile. If brand-new:
cd /opt/OpenMontage_Voicebox
make tweak-server                    # foreground
# OR
nohup .venv/bin/python -m uvicorn tweak_server.app:app \
    --host 0.0.0.0 --port 8901 \
    > /tmp/tweak-server.log 2>&1 &

# Open the form:
#   http://localhost:8901/projects/the-refactor-serenade/tweak
```

Three flows in one app:

1. **Async submit**: POST `/api/projects/{id}/tweak` → HTTP 202 + `job_id` in
   <100ms; the actual MCP render runs in a background asyncio task.
2. **Progress SSE**: GET `/api/projects/{id}/jobs/{job_id}/events` — emits
   `progress` events every 1s from local JobStore + best-effort pass-through
   of MCP's `/render-progress/{job_id}` bytes. Stream closes when the job
   reaches `completed` or `failed` (terminal event).
3. **Asset upload**: POST `/api/projects/{id}/assets/{subdir}` (multipart) →
   saves to `projects/<id>/assets/<subdir>/<safe-name>`. MIME whitelisted,
   size-capped per subdir, dedupes by suffix.

## Architecture

```
Browser SPA / openclaw client
        │
        │  CORS preflight + JSON-RPC-style REST
        ▼
┌─────────────────────────────────────────┐
│ tweak_server  (:8901, FastAPI)          │
│  app.py     — routes                     │
│  props_schema.py — TweakPayload +       │
│                  field whitelist         │
│  mcp_client.py — JSON-RPC over          │
│                  streamable-http         │
│                  (Bearer + session-id)   │
│  jobs.py    — Job + JobStore             │
│              (in-memory, thread-safe)    │
│  queue.py   — submit_render_job         │
│              (asyncio.create_task)       │
│  progress.py — SSE bridge               │
│               (local heartbeat +        │
│                MCP pass-through)         │
│  assets.py  — upload / list / delete     │
│  auth.py    — X-Tweak-Token bearer       │
│              + ?token= for SSE           │
└────────────────┬────────────────────────┘
                 │
                 │  tools/call
                 │  execute_tool(video_compose, remotion_render)
                 ▼
        ┌────────────────────┐
        │ MCP server (:8900) │
        │ remotion render    │  blocks 30-90s
        └────────┬───────────┘
                 │
                 │  writes
                 ▼
projects/<project-id>/renders/tweak-<ISO-timestamp>.mp4
projects/<project-id>/decision_log_tweak_rev<NNN>.json
projects/<project-id>/decision_log_tweak_rev_snapshot_<sid>.json
```

## Git history (in order)

```
e6d59e0  feat(tweak-server): CORS middleware for openclaw SPA integration
8e919bd  fix(tweak-server): emit progress event every tick, not just on change
d0c8607  fix(tweak-server): SSE heartbeat from local JobStore
7ec20d2  feat(tweak-server): project asset upload/list/delete
1a9c6a1  feat(tweak-server): add SSE progress bridge (status + events endpoints)
e3eac7f  feat(tweak-server): async job queue
626c3dd  feat(tweak-server): initial sidecar MCP client for end-user render tweaks
```

The initial sidecar was synchronous. The next three commits (B → A → C)
were built in parallel by three worktree-isolated agents and cherry-picked
in that order. The two `fix:` commits are post-merge corrections:

| Bug | Fix commit |
|---|---|
| SSE endpoint connected to MCP but emitted nothing because MCP doesn't know tweak-server's job_ids (we bypass `create_remotion_video_share`) | `d0c8607` — add local JobStore heartbeat as authoritative source |
| During the 130s+ of stable "rendering" phase, no events fired — UI looked dead | `8e919bd` — emit on every tick, not just on state change |

## Files

```
tweak_server/                  2,403 lines total
├── __init__.py
├── app.py            481     FastAPI app + CORS + 10 routes + lifespan
├── assets.py         176     upload/list/delete + MIME whitelist + size caps
├── jobs.py            75     Job dataclass + JobStore + thread singleton
├── queue.py           85     submit_render_job — wraps MCP render in asyncio task
├── progress.py       191     SSE bridge (status + events endpoints)
├── mcp_client.py     267     JSON-RPC over streamable-http + session-id echo + envelope unwrap
├── props_schema.py   250     TweakPayload + merge_into_template + field whitelist
├── auth.py            43     X-Tweak-Token + ?token= for EventSource
├── README.md                  operator-facing quickstart
└── static/
    ├── tweak.html     80     form scaffold + progress block + assets block
    ├── tweak.css     175     dark theme + progress + asset styles
    └── tweak.js     616     init / field builders / submit / SSE progress / asset upload
```

## API contract

All authenticated endpoints require `X-Tweak-Token` header **OR** `?token=`
query param. SSE endpoints accept either (EventSource can't set headers).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET    | `/`                                      | no  | health + capability snapshot |
| GET    | `/projects/{id}/tweak`                   | no  | form HTML |
| GET    | `/api/projects/{id}`                      | yes | current template + schema metadata (JSON) |
| POST   | `/api/projects/{id}/tweak`                | yes | submit tweak → enqueue MCP render (returns HTTP 202 + `job_id`) |
| GET    | `/api/projects/{id}/jobs`                 | yes | list jobs for project (newest first) |
| GET    | `/api/projects/{id}/jobs/{job_id}`        | yes | current Job state (JSON snapshot) |
| GET    | `/api/projects/{id}/jobs/{job_id}/events` | yes | SSE stream (progress + terminal events) |
| POST   | `/api/projects/{id}/assets/{subdir}`      | yes | multipart upload → save to projects/.../assets/<subdir>/ |
| GET    | `/api/projects/{id}/assets`               | yes | list assets grouped by subdir |
| DELETE | `/api/projects/{id}/assets/{subdir}/{filename}` | yes | delete one asset |
| GET    | `/renders/{id}/{filename}.mp4`            | no  | serve rendered mp4 for preview-in-page |
| GET    | `/static/{tweak.html, tweak.js, tweak.css}` | no  | static assets |

### POST `/api/projects/{id}/tweak`

Request:
```json
{
  "theme": "flat-motion-graphics",
  "cuts": [{"id": "punchline-text", "fontSize": 120, "color": "#FF6B6B"}],
  "audio": {"music": {"volume": 0.25}},
  "comment": "What did you change and why?"
}
```

Response (HTTP 202):
```json
{
  "job_id": "f254925af101",
  "status": "queued",
  "project_id": "the-refactor-serenade",
  "staging_id": "tweak-f254925af101",
  "decision_log": "decision_log_tweak_rev006.json",
  "comment": "...",
  "merged_cuts_touched": ["punchline-text"]
}
```

Failure modes:
- 400 invalid_payload (schema validation)
- 400 merge_failed (e.g. cut id not found, type mismatch)
- 404 project_not_found
- 502 mcp_render_failed (MCP unreachable / 401 / 5xx) — note: only fires for
  the initial MCP `initialize`; per-render failures land on the Job as
  `status="failed"` + `error="..."` and are surfaced via SSE terminal event.

### GET `/api/projects/{id}/jobs/{job_id}/events` (SSE)

Server-Sent Events. Each event:
```
event: progress\n
data: {"event":"render_progress","render_job_id":"...","phase":"starting","status":"rendering","percent":0.0,"message":"calling MCP","staging_id":"...","output_path":"..."}\n
\n
```

Plus terminal event when job finishes:
```
event: terminal\n
data: {"event":"render_progress","status":"completed","percent":100.0,"result":{...},"output_path":"..."}\n
\n
```

Two sources are merged in `progress.py`:

1. **Local JobStore heartbeat** — authoritative. Polls every 1s; emits when
   status/phase/percent changes. Doesn't depend on MCP knowing the job_id.
2. **MCP `/render-progress/{job_id}` pass-through** — best-effort. Forwards
   bytes raw when MCP has its own SSE (only if the job went through
   `create_remotion_video_share`; tweak-server's own job_ids don't trigger
   MCP-side events).

Stream closes when the JobStore reports `completed`/`failed`. Polling-only
fallback: `GET /api/projects/{id}/jobs/{job_id}` (one-shot JSON snapshot).

### Field whitelist (`props_schema.py`)

| Field | Applies to | Range |
|---|---|---|
| `theme` | top-level | 5 yaml names (see VALID_THEMES) |
| `cuts[].text` | text_card / hero_title / stat_card / callout | ≤ 500 chars |
| `cuts[].fontSize` | text_card / hero_title | 24 – 200 |
| `cuts[].color` | text_card / hero_title | `#RRGGBB[AA]` |
| `cuts[].backgroundColor` | any cut | `#RRGGBB[AA]` |
| `cuts[].animation` | image / video cuts (`source` present, no `type`) | enum: zoom-in / pan-down / ken-burns / none |
| `cuts[].in_seconds`/`out_seconds` | any cut | 0 – 600; out > in |
| `audio.narration.volume` | top-level | 0.0 – 1.0 |
| `audio.music.volume` | top-level | 0.0 – 1.0 |
| `audio.music.fadeIn/Out` | top-level | 0.0 – 3.0 s |
| `audio.music.offsetSeconds` | top-level | 0.0 – 30 s |

**Forbidden** (rejected with 400 — never silently dropped):
`cuts[].id`, `cuts[].source`, `cuts[].type`, `audio.*.src`, any stat/chart
fields. New cut creation/deletion not supported in this version.

## Auth

- `TWEAK_SERVER_BEARER` env var enables auth. Empty = auth off (development).
- Clients send `X-Tweak-Token: <token>` header on every request.
- SSE endpoints accept `?token=<token>` query param instead (EventSource
  cannot set headers — see `auth.py:_sse_token_auth`).
- Constant-time compare via `hmac.compare_digest`.

## CORS

`TWEAK_SERVER_CORS_ORIGINS` env var (comma-separated). Default
`http://localhost:18789,http://127.0.0.1:18789` (openclaw-gateway web UI).
Set to `*` for development.

Exposed headers: `Content-Disposition` (for filename download).

## Decision log (append-only)

Each successful submit writes:

```
projects/<id>/decision_log_tweak_rev<NNN>.json           # metadata only
projects/<id>/decision_log_tweak_rev_snapshot_<sid>.json # full props used
```

`_append_decision_log` in `app.py` does NOT mutate the existing
`decision_log.json` — pure append per `CLAUDE.md` invariant 6.

## Env vars

| Name | Default | Purpose |
|---|---|---|
| `MCP_HTTP_URL` | `http://127.0.0.1:8900` | MCP server base URL |
| `MCP_API_TOKEN` | (none) | MCP Bearer token |
| `TWEAK_SERVER_HOST` | `127.0.0.1` | uvicorn bind |
| `TWEAK_SERVER_PORT` | `8901` | uvicorn port |
| `TWEAK_SERVER_BEARER` | (none, empty = auth off) | clients send as `X-Tweak-Token` |
| `TWEAK_RENDER_TIMEOUT_S` | `600` | httpx read timeout (10 min) |
| `TWEAK_SERVER_LOG_LEVEL` | `INFO` | logging verbosity |
| `TWEAK_SERVER_CORS_ORIGINS` | `http://localhost:18789,http://127.0.0.1:18789` | CORS allow-list (`*` for dev) |
| `TWEAK_SERVER_PROJECT_ID` | `the-refactor-serenade` | default project if route doesn't pin one |

## MCP call shape (for reference)

**Call**: `execute_tool` → `video_compose(operation="remotion_render", ...)`

```json
{
  "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": {
    "name": "execute_tool",
    "arguments": {
      "tool_name": "video_compose",
      "inputs": {
        "operation": "remotion_render",
        "edit_decisions": { /* merged props */ },
        "output_path": "projects/<id>/renders/tweak-<ISO>.mp4",
        "staging_id": "tweak-<job_id>",
        "remotion_timeout_ms": 600000
      }
    }
  }
}
```

Three MCP gotchas discovered during integration (worth remembering):

1. `Mcp-Session-Id` header — must be captured from `initialize` response and
   echoed on every subsequent request.
2. `Accept` header must be `application/json, text/event-stream` —
   otherwise MCP returns 406.
3. `execute_tool` parameter is `inputs` (NOT `arguments`) — the inner dict
   that becomes `video_compose`'s kwargs.

5. Tool responses are wrapped:
   ```json
   {"content": [{"type": "text", "text": "<json string>"}], "isError": bool}
   ```
   `mcp_client._unwrap()` parses the inner JSON.

## End-to-end smoke test (verified)

```bash
TOKEN="local-dev-tweak-token-2026"
PROJECT="the-refactor-serenade"

# 1. Submit (should return 202 in <100ms with job_id)
JOB_ID=$(curl -sX POST -H "X-Tweak-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"cuts":[{"id":"punchline-text","fontSize":108,"color":"#FFE0B2"}]}' \
  "http://127.0.0.1:8901/api/projects/$PROJECT/tweak" \
  | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# 2. SSE (Python httpx — curl buffers)
python3 -c "
import httpx, time
url = f'http://127.0.0.1:8901/api/projects/$PROJECT/jobs/$JOB_ID/events'
with httpx.stream('GET', url, headers={'X-Tweak-Token': '$TOKEN'}, timeout=5) as r:
    for line in r.iter_lines():
        if line.startswith('data:'):
            print(line)
            break  # one event is enough to prove SSE works
"

# 3. Asset upload
curl -sX POST -H "X-Tweak-Token: $TOKEN" \
  -F "file=@/tmp/some.png;type=image/png" \
  "http://127.0.0.1:8901/api/projects/$PROJECT/assets/images"
```

## openclaw-gateway integration

- CORS enabled (`TWEAK_SERVER_CORS_ORIGINS`).
- Auth via `X-Tweak-Token` (header) or `?token=` (for SSE).
- Browser SPA: use `new EventSource(url + '?token=' + TOKEN)` for progress.
- Electron: same, plus direct filesystem access to mp4 if useful.
- Reverse-proxy / plugin paths NOT implemented (would require openclaw
  internals exploration).

## Known limitations / non-goals (do not add unless asked)

- No A/B batch rendering ("tweak then render for N asset manifests")
- No rollback UI ("revert to rev003")
- No chart / stat-card data editing (would need per-scene-type schema work)
- No new asset upload wired into the form's cut.source picker yet
  (uploaded files land in `projects/<id>/assets/<subdir>/` but the form
  doesn't yet let you pick them — assets list is shown read-only)
- No rate limiting (relies on MCP's fair queue + Bearer auth)
- JobStore is in-memory (restart loses history — decision logs on disk survive)
- SSE doesn't include frame-level progress (only phase + status + message);
  for that we'd need to switch to `create_remotion_video_share` (heavier)

## Tests

```bash
# Compile all
python -m py_compile tweak_server/{__init__,app,mcp_client,auth,props_schema,jobs,queue,progress,assets}.py

# JS syntax
node --check tweak_server/static/tweak.js
```

No automated pytest suite — this codebase has none yet. Manual smoke tests
are the verification path; see `tweak_server/README.md` for curl snippets.

## Things NOT to do

- Don't add `create_remotion_video_share` integration — that path requires
  asset_manifest + proposal_packet + scene_plan, overkill for "tweak one
  prop and re-render".
- Don't add persistent JobStore (SQLite/Redis) until in-memory is shown to
  be a real problem (it isn't yet — JobStore is for in-flight progress only;
  history is in `decision_log_tweak_rev*.json`).
- Don't change the SSE event format (`event: progress` / `event: terminal`)
  without bumping a version — openclaw clients depend on it.
- Don't change the append-only contract for decision logs.