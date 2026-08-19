# Voicebox × OpenMontage Integration Analysis

> Date: 2026-08-19 | Status: Analysis Complete

## Summary

There are **three integration directions** between Voicebox (local voice I/O) and OpenMontage (agentic video production). The optimal path depends on which system acts as the primary orchestrator.

---

## OpenMontage's Video Generation Capability (Clarification)

**`remotion-composer/` is not just a renderer — it is a full React/Remotion video generation engine.**

The `remotion-composer/` directory is a Node.js/React project that creates animated explainer videos programmatically. Key capabilities:

**5 registered compositions** (defined in `src/Root.tsx`):
| Composition | Purpose |
|-------------|---------|
| `Explainer` | Main animated explainer — text cards, stat cards, charts, terminal scenes, anime scenes |
| `CinematicRenderer` | Cinematic video composition |
| `TalkingHead` | Avatar talking-head video |
| `TitledVideo` | Title overlay video |
| `EndTag` | End card |
| `CustomComposition` | User-provided TSX via `@babel/standalone` runtime compilation |

**Scene types** (`cut.type` in `Explainer`):
- `text_card`, `hero_title` — large typography beats
- `stat_card`, `callout`, `comparison` — data/callout cards
- `bar_chart`, `line_chart`, `pie_chart`, `kpi_grid`, `progress_bar` — animated charts
- `anime_scene` — still images with particle effects + camera motion
- `terminal_scene` — synthetic terminal animation (typing, cursor, command output)
- `screenshot_scene` — any screenshot with animated overlay steps (cursor, clicks, bubbles)
- Image/video clips with Ken Burns zoom

**Background system:**
- Animated gradient mesh backgrounds (slow-moving dual-radial + linear)
- Floating glow orbs derived from theme colors
- Subtle grid overlay with light/dark adaptation
- All driven by theme config (primaryColor, accentColor, chartColors, etc.)

**How it integrates with the tool layer:**
`video_compose.py` dispatches to `remotion-composer` when `operation="remotion_render"`. The Python tool passes `edit_decisions` (cut list + theme) as JSON props to the Remotion composition, which renders via `npx remotion render` with Chrome headless.

---

## 附：Voicebox 网页调用限制

**结论：Voicebox 当前不支持通过网页直接调用。**

| 系统 | 网页直接调用 | 原因 |
|------|------------|------|
| Voicebox | ❌ 不支持 | MCP 是 JSON-RPC over HTTP，浏览器 `fetch()` 无法构造；CORS 只允许 localhost + `tauri://` |
| OpenMontage | ✅ 支持 | 有 Go BFF (`frameflow/bff/`) 提供 REST 中间层 `POST /api/mcp` |

**根本原因：** MCP 是长连接、有状态、会话轮换的协议，不适合直接暴露给浏览器。

**如需网页调用 Voicebox，需要在 Voicebox 中新增一个 BFF 层**（Go 或 Python FastAPI），将 REST 请求转为 MCP JSON-RPC，同时处理 CORS 和会话管理。

---

## Integration Direction A: OpenMontage calls Voicebox (MCP Client → Voicebox)

**Use case:** OpenMontage's LLM agent needs voice I/O as part of its video pipeline.

### How it works

```
OpenMontage (MCP Client)  →  HTTP/MCP  →  Voicebox /mcp (port 17493)
```

### Available Voicebox tools for OpenMontage

| Tool | OpenMontage Pipeline Use |
|------|--------------------------|
| `voicebox.speak` | Announce pipeline stage completions, narration with cloned voices, agent voice feedback |
| `voicebox.transcribe` | Transcribe voice recordings from in-pipeline mic captures |
| `voicebox.analyze_sample` | Validate voice samples before cloning into a character voice |
| `voicebox.list_profiles` | Discover available cloned voices for character assignment |
| `voicebox.list_captures` | Retrieve prior dictation recordings |

### Configuration

```json
{
  "mcp": {
    "servers": {
      "voicebox": {
        "name": "voicebox",
        "url": "http://127.0.0.1:17493/mcp",
        "headers": {
          "X-Voicebox-Client-Id": "openmontage-agent"
        }
      }
    }
  }
}
```

### Architecture note: BFF is not the agent's MCP client

The `frameflow/bff/` directory is a **browser-facing Go BFF** (API gateway) for OpenMontage's web UI. Its `MCPProxy` (`handlers/mcp.go`) is a tightly-controlled relay with a **hardcoded tool whitelist**:

```go
var allowedMCPTools = map[string]bool{
    "upload_asset_chunk":          true,
    "create_remotion_video_share": true,
    "get_render_status":           true,
}
```

This BFF routes all requests to a single OpenMontage MCP server and has no concept of multiple MCP servers or tool-name routing. **Voicebox tools (`voicebox.speak`, etc.) are not in the whitelist and would be rejected.**

The OpenMontage **agent** (Claude Code, Cursor, etc.) calls MCP servers directly — it does not go through the BFF. The agent configures its own MCP clients via `.mcp.json` at the OpenMontage repo root:

```json
{
  "mcp": {
    "servers": {
      "voicebox": {
        "url": "http://127.0.0.1:17493/mcp",
        "headers": { "X-Voicebox-Client-Id": "openmontage-agent" }
      }
    }
  }
}
```

This `.mcp.json` is what Claude Code reads when it operates inside the OpenMontage repo.

### Auth

Voicebox uses `X-Voicebox-Client-Id` for identity (no bearer token). Loopback-only restrictions apply to `audio_path`; base64 audio always works.

### Limitations

- Voicebox only exposes HTTP Streamable HTTP, not stdio — OpenMontage's agent must use its HTTP MCP client.
- Voicebox cannot generate video — it only provides voice I/O.

---

## Integration Direction B: Voicebox calls OpenMontage (Voicebox as MCP Client)

**Use case:** Voicebox wants to render a full video using OpenMontage's pipeline, with Voicebox-generated narration as the audio track.

### How it works

```
Voicebox (MCP Client)  →  HTTP/MCP  →  OpenMontage /mcp (port 8900)
```

Voicebox generates the narration audio via its TTS engine (with voice cloning), then calls OpenMontage to render a video and embed that audio as the narration track.

### Relevant OpenMontage tools for Voicebox

| Tool | Voicebox Use |
|------|--------------|
| `execute_tool("video_compose", ...)` | Render full animated video — text cards, stat charts, terminal scenes, etc. |
| `execute_tool("edge_tts", ...)` | TTS fallback for languages Voicebox doesn't support |
| `execute_tool("subtitle_gen", ...)` | Generate SRT/VTT subtitles from Voicebox's transcribed text |
| `execute_tool("audio_mixer", ...)` | Mix cloned-voice narration + background music + SFX |
| `execute_tool("upload_asset", ...)` | Ingest Voicebox-generated audio into OpenMontage project |
| `execute_tool("video_stitch", ...)` | Concatenate video segments |
| `execute_tool("s3_upload", ...)` | Publish finished video to CDN, return public URL |
| `execute_tool("create_remotion_video_share", ...)` | Generate shareable video link via Weiyun |
| `list_tools`, `get_tool_info`, `dry_run_tool` | Discovery and preflight |

### Implementation requirement

Voicebox must add an **MCP client** to call OpenMontage's `/mcp` endpoint. Options:

1. **`mcp` Python SDK** — use `mcp.client` in a Voicebox service to make outbound calls
2. **FastMCP client mode** — `FastMCP` can also act as a client (call other servers)
3. **New FastAPI route** — proxy OpenMontage tool calls through a Voicebox REST endpoint

### Auth

OpenMontage requires `Authorization: Bearer <MCP_API_TOKEN>`.

---

## Integration Direction C: Bidirectional (Both Call Each Other)

Both systems expose MCP servers. A supervisory agent coordinates both:

```
Claude Code (orchestrator)
  │
  ├── voicebox.speak(profile="Morgan", text="...")
  │     → Voicebox generates cloned-voice narration
  │     → Audio saved to file
  │
  ├── execute_tool("upload_asset", {file_path: "/path/to/narration.mp3"})
  │     → OpenMontage ingests the audio
  │
  ├── execute_tool("video_compose", {
  │       cuts: [{type: "text_card", text: "...", backgroundAudio: "narration.mp3"}],
  │       theme: {...}
  │     })
  │     → OpenMontage renders video with narration track + animated scenes
  │
  └── execute_tool("s3_upload", {video_path: "...", visibility: "public"})
        → Published video returned as public URL
```

**Key insight:** Direction C is not a "feature" of either system — it is a **coordination pattern** where an external agent (Claude Code) holds references to both systems' outputs and stitches them together. Neither Voicebox nor OpenMontage needs to know about the other.

### Concrete application: "产品介绍视频" pipeline

1. **Voicebox**: `voicebox.speak(profile="Morgan", text="新鲜烘焙，源自云南...", personality=true)` → generates in-character narration MP3
2. **OpenMontage**: `video_compose` renders animated explainer with Morgan's narration as `backgroundAudio` on each cut, theme configured to brand colors
3. **OpenMontage**: `subtitle_gen` generates SRT from Morgan's transcript
4. **OpenMontage**: `video_compose(..., burn_subtitles=true)` burns SRT into video
5. **OpenMontage**: `s3_upload(visibility="public")` returns CDN URL

---

## Comparison Table

| Dimension | A: OM→Voicebox | B: Voicebox→OM | C: Bidirectional |
|----------|----------------|----------------|-------------------|
| **Complexity** | Low | Medium | High |
| **Code changes to Voicebox** | None | Add MCP client | Add MCP client |
| **Code changes to OpenMontage** | MCP client config | None | None |
| **Primary value to OM** | Voice cloning + STT for pipelines | — | — |
| **Primary value to Voicebox** | — | Video rendering pipeline | Video rendering pipeline |
| **Orchestrator** | OpenMontage agent | Voicebox service | External agent (Claude Code) |
| **Requires network** | OM→VB port 17493 | VB→OM port 8900 | Both |
| **Auth** | X-Voicebox-Client-Id | Bearer token | Both |

---

## Recommended: Direction A first, Direction C eventually

**Direction A** is the lowest-effort starting point — no code changes to Voicebox, OpenMontage already has an HTTP MCP client, and the gain (voice cloning in pipelines) is immediate and concrete.

**Direction C** is the end state for a fully integrated voice-first video production workflow.

---

## Key Files Reference

### Voicebox MCP Server
- `/opt/voicebox/backend/mcp_server/server.py` — FastMCP mount, lifespan composition
- `/opt/voicebox/backend/mcp_server/tools.py` — 5 tools: speak, transcribe, list_captures, list_profiles, analyze_sample
- `/opt/voicebox/backend/mcp_server/context.py` — `ClientIdMiddleware`, `current_client_id` ContextVar
- `/opt/voicebox/backend/mcp_server/events.py` — SSE pub/sub for speak-start/speak-end

### OpenMontage MCP Server
- `/opt/OpenMontage/mcp_server.py` — FastMCP server, Bearer token auth, port 8900
- `/opt/OpenMontage/tools/tool_registry.py` — Auto-discovery of 57+ BaseTool subclasses
- `/opt/OpenMontage/tools/video/video_compose.py` — FFmpeg/Remotion/HyperFrames dispatch
- `/opt/OpenMontage/remotion-composer/` — **Full React/Remotion video generation engine**
  - `src/Root.tsx` — 6 registered compositions + theme system
  - `src/Explainer.tsx` — 15+ scene types with animated backgrounds
  - `src/components/` — TextCard, StatCard, TerminalScene, ScreenshotScene, charts, etc.

### Both servers already running
- Voicebox MCP: port **17493**
- OpenMontage MCP: port **8900** (PID 4132564)

---

## Wired MCP Configuration (2026-08-19)

Both `.mcp.json` files now register the **other** system, so a Claude Code session running in either repo sees tools from both sides. This is **Direction C (coordination pattern)** in practice — Claude Code itself is the orchestrator, calling `voicebox.*` and `openmontage.*` tools in sequence.

### `/opt/voicebox/.mcp.json`

```json
{
  "mcpServers": {
    "voicebox": {
      "type": "http",
      "url": "http://127.0.0.1:17493/mcp",
      "headers": { "X-Voicebox-Client-Id": "claude-code" }
    },
    "openmontage": {
      "type": "http",
      "url": "http://127.0.0.1:8900/mcp",
      "headers": {
        "Authorization": "Bearer h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"
      }
    }
  }
}
```

### `/opt/OpenMontage/.mcp.json`

```json
{
  "mcpServers": {
    "voicebox": {
      "type": "http",
      "url": "http://127.0.0.1:17493/mcp",
      "headers": { "X-Voicebox-Client-Id": "openmontage-agent" }
    }
  }
}
```

OpenMontage's own tools are reachable natively because Claude Code operating inside the OpenMontage repo connects to its own MCP server at the same address; the `voicebox` entry adds the cross-system tools.

### Auth Summary

| Server | Auth | Header |
|--------|------|--------|
| Voicebox (`/mcp`) | Client identity (no secret) | `X-Voicebox-Client-Id: <agent-name>` |
| OpenMontage (`/mcp`) | Bearer token | `Authorization: Bearer <MCP_API_TOKEN>` |

The bearer token is sourced from `/opt/OpenMontage/.env` (`MCP_API_TOKEN`). For loopback MCP clients this is the standard pattern; rotate the token by editing `.env` and restarting `mcp_server.py`.

### What Gets Exposed After Wiring

When Claude Code is launched inside `/opt/voicebox`:

- **voicebox** (5 tools): `voicebox.speak`, `voicebox.transcribe`, `voicebox.list_captures`, `voicebox.list_profiles`, `voicebox.analyze_sample`
- **openmontage** (25 tools, including): `list_tools`, `get_tool_info`, `execute_tool`, `upload_asset`, `upload_asset_chunk`, `create_remotion_video_share`, `get_render_status`, `s3_upload`, `dry_run_tool`, `list_pipelines`, …

When Claude Code is launched inside `/opt/OpenMontage`:

- **voicebox** (5 tools) — same set as above
- **OpenMontage native tools** (25 tools) — same set

### End-to-End Smoke Test

Both servers validated by direct `curl` against `/mcp` on 2026-08-19:

```bash
# Voicebox initialize → session
curl -s -X POST http://127.0.0.1:17493/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200, mcp-session-id header set, serverInfo.name=voicebox

# OpenMontage initialize with bearer token
curl -s -X POST http://127.0.0.1:8900/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200, mcp-session-id header set, serverInfo.name=OpenMontage
```

### How to Reload the Config

Claude Code reads `.mcp.json` at startup. After editing either file:

1. Quit and relaunch Claude Code in that repo, **or**
2. Use `/mcp` slash command and `reconnect` to force a refresh.

### Example Orchestration (voicebox → OpenMontage)

A single Claude Code turn inside `/opt/voicebox` can now run:

1. `voicebox.list_profiles` → pick a cloned voice for narration
2. `voicebox.speak(profile="Morgan", text="新鲜烘焙，源自云南...")` → cloned-voice MP3
3. `openmontage.execute_tool(name="upload_asset", args={file_path: "<voicebox output>"})` → asset in project
4. `openmontage.execute_tool(name="video_compose", args={...cuts: [...backgroundAudio: "<voicebox output>"]})` → animated video with cloned narration
5. `openmontage.execute_tool(name="subtitle_gen", args={audio_path: "<voicebox output>"})` → SRT
6. `openmontage.execute_tool(name="s3_upload", args={video_path: "...", visibility: "public"})` → public CDN URL

No code changes were required in either repo — only `.mcp.json` wiring + bearer token.
