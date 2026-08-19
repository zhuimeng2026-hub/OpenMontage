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
│   └── voicebox.mcp.json                ← Voicebox-side wiring (apply into /opt/voicebox/.mcp.json)
└── docs/
    └── openmontage-integration.md       ← full analysis + smoke test + usage examples
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

## Servers That Must Be Running

| Server | Port | Start command |
|--------|------|---------------|
| Voicebox | 17493 | `cd /opt/voicebox && just dev-backend` |
| OpenMontage | 8900 | `cd /opt/OpenMontage && python mcp_server.py` |

Both expose **Streamable HTTP** transport at `/mcp`.

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
```

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
| `docs/openmontage-integration.md` | `/opt/voicebox/docs/openmontage-integration.md` | Full analysis doc |

The Voicebox-side source files in `/opt/voicebox/` remain live so that Claude
Code operating directly in `/opt/voicebox` keeps working without depending on
this branch being checked out somewhere.