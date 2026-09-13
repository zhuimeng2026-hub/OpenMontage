# OpenMontage × Voicebox MCP Integration

This branch (`OpenMontage_Voicebox`) is the **canonical source** for the MCP
wiring that lets Claude Code operating in either repo call tools from both
systems. The integration is symmetric — neither repo needed code changes, only
`.mcp.json` entries + this branch's docs.

## Layout

```
OpenMontage_Voicebox/                    ← git worktree, branch OpenMontage_Voicebox
├── .mcp.json                            ← OpenMontage-side wiring (live in this worktree)
├── integration/
│   ├── README.md                        ← this file
│   ├── voicebox.mcp.json                ← Voicebox-side wiring (apply into /opt/voicebox/.mcp.json)
│   └── claude-video.mcp.json            ← claude-video-side wiring (apply into /opt/claude-video/.mcp.json)
└── docs/
    ├── openmontage-integration.md       ← full Voicebox analysis + smoke test + usage examples
    └── claude-video-prerequisites.md    ← claude-video host prerequisites + smoke test
```

## What Each `.mcp.json` Does

### `.mcp.json` (this worktree, OpenMontage-side)

When Claude Code runs inside this worktree, it reads `.mcp.json` and connects
to:

| Server | URL | Auth |
|--------|-----|------|
| `voicebox` | `http://127.0.0.1:17493/mcp` | `X-Voicebox-Client-Id` header (no secret) |

Claude Code also picks up the OpenMontage MCP server at
`http://127.0.0.1:8900/mcp` natively (since the running MCP server is part of
this repo's dev environment), so all OpenMontage tools are reachable. With this
entry, the cross-system `voicebox.*` tools are also reachable.

### `integration/voicebox.mcp.json` (Voicebox-side, reference)

Copy the contents of `integration/voicebox.mcp.json` into
`/opt/voicebox/.mcp.json` to give Claude Code operating inside the Voicebox
repo access to both sides:

| Server | URL | Auth |
|--------|-----|------|
| `voicebox` | `http://127.0.0.1:17493/mcp` | `X-Voicebox-Client-Id` header |
| `openmontage` | `http://127.0.0.1:8900/mcp` | `Authorization: Bearer <MCP_API_TOKEN>` |

The bearer token is sourced from `/opt/OpenMontage/.env` (`MCP_API_TOKEN=...`).
Rotate it by editing that env file and restarting the OpenMontage MCP server.

### `integration/claude-video.mcp.json` (claude-video-side, reference)

Copy the contents of `integration/claude-video.mcp.json` into
`/opt/claude-video/.mcp.json` to give Claude Code operating inside the
claude-video repo access to all three systems:

| Server | Transport | Target | Auth |
|--------|-----------|--------|------|
| `claude-video` | stdio | child process — `/opt/claude-video/skills/watch/scripts/mcp_server.py` | none (stdio) |
| `openmontage` | Streamable HTTP | `http://127.0.0.1:8900/mcp` | `Authorization: Bearer <MCP_API_TOKEN>` |
| `voicebox` | Streamable HTTP | `http://127.0.0.1:17493/mcp` | `X-Voicebox-Client-Id` header |

Same bearer token source as above. The stdio entry does **not** need a
running daemon — Claude Code spawns the script per session and feeds it
JSON-RPC over stdin/stdout.

### Additive wiring — OpenMontage-side `.mcp.json`

To let a Claude Code session running inside this worktree call claude-video
as a tool, add a `claude-video` entry to the existing `.mcp.json` next to
the `voicebox` entry:

```json
{
  "mcpServers": {
    "voicebox": {
      "type": "http",
      "url": "http://127.0.0.1:17493/mcp",
      "headers": {
        "X-Voicebox-Client-Id": "openmontage-agent"
      }
    },
    "claude-video": {
      "type": "stdio",
      "command": "python3",
      "args": ["/opt/claude-video/skills/watch/scripts/mcp_server.py"],
      "env": {}
    }
  }
}
```

This is the OM-side counterpart to `integration/claude-video.mcp.json`
(which is the claude-video-side mirror). After editing, restart Claude
Code in the worktree or use `/mcp` → `reconnect`.

## Servers That Must Be Running

| Server | Port | Transport | Start command |
|--------|------|-----------|---------------|
| Voicebox | 17493 | Streamable HTTP `/mcp` | `cd /opt/voicebox && just dev-backend` |
| OpenMontage | 8900 | Streamable HTTP `/mcp` | `cd /opt/OpenMontage && python mcp_server.py` |
| claude-video | — | stdio (per Claude Code session) | `python3 /opt/claude-video/skills/watch/scripts/mcp_server.py` |

The claude-video entry is a stdio transport — Claude Code spawns it as a
child process per session; there is no persistent daemon to start. The
single third-party Python dependency is the `mcp` SDK
(`pip install --user -r /opt/claude-video/requirements.txt`). See
[`docs/claude-video-prerequisites.md`](../docs/claude-video-prerequisites.md)
for the full prerequisite list (ffmpeg, yt-dlp, optional Whisper API key).

## Smoke Test

After applying the configs:

```bash
# Voicebox
curl -s -X POST http://127.0.0.1:17493/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200, mcp-session-id header set, serverInfo.name=voicebox (5 tools)

# OpenMontage
curl -s -X POST http://127.0.0.1:8900/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# → 200, mcp-session-id header set, serverInfo.name=OpenMontage (25 tools)

# claude-video (stdio — spawn the script and send initialize over stdin)
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | python3 /opt/claude-video/skills/watch/scripts/mcp_server.py
# → on stdout: JSON-RPC `result` with serverInfo.name=watch and capabilities;
#   on stderr: the stdlib logging output. Exit code 0.
```

See [`docs/claude-video-prerequisites.md`](../docs/claude-video-prerequisites.md)
for the full claude-video prerequisite list and a second smoke test that
drives the `watch` tool end-to-end with a real URL.

## Why No Code Changes Were Required

Both systems already expose MCP servers (Voicebox via FastMCP at `/mcp`,
OpenMontage via its own FastMCP server at `/mcp`). Adding the **other** server
to `.mcp.json` is sufficient because **Claude Code itself is the orchestrator**
— it calls `voicebox.*` and OpenMontage tools in sequence inside a single
turn. See `docs/openmontage-integration.md` for the full orchestration
examples (clone voice → upload asset → render video → publish CDN).

## Reloading After Config Changes

Claude Code reads `.mcp.json` at startup. After editing either file:

1. Quit and relaunch Claude Code in the affected repo, **or**
2. Use `/mcp` slash command → `reconnect` to force a refresh in-session.

## Provenance

| File | Source | Note |
|------|--------|------|
| `.mcp.json` | `/opt/OpenMontage/.mcp.json` | Identical, for worktree-internal Claude Code |
| `integration/voicebox.mcp.json` | `/opt/voicebox/.mcp.json` | Mirror for the other repo |
| `integration/claude-video.mcp.json` | `/opt/claude-video/.mcp.json` | Mirror for the claude-video repo (stdio + HTTP) |
| `docs/openmontage-integration.md` | `/opt/voicebox/docs/openmontage-integration.md` | Full analysis doc (Voicebox ↔ OM) |
| `docs/claude-video-prerequisites.md` | (new) | Host prerequisites + smoke test for the claude-video stdio entry |

The Voicebox-side source files in `/opt/voicebox/` remain live so that Claude
Code operating directly in `/opt/voicebox` keeps working without depending on
this branch being checked out somewhere. The same is true of
`/opt/claude-video/` — applying the mirror there does not require this
worktree to be checked out.