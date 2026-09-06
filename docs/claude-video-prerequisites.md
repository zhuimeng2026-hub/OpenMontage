# claude-video Integration Prerequisites

> Audience: AI coding agents (Claude Code, Cursor, Cline, …) wiring the
> `claude-video` MCP server into an OpenMontage session via
> `/opt/OpenMontage_Voicebox/integration/claude-video.mcp.json`.

claude-video is **agent-native** — its only Python dependency is the `mcp`
SDK; the rest is stdlib + `yt-dlp` + `ffmpeg`. After the prerequisites below
are met, the server runs without any API key for the common path (captioned
videos). Whisper is only reached when a video has no captions.

## TL;DR — minimum to make the snippet work

```bash
# 1. ffmpeg + yt-dlp on PATH (claude-video shells out to both)
command -v ffmpeg yt-dlp
# Expect: both print a path. If either is missing, install via your package
# manager (apt/brew) or `pip install --user yt-dlp`.

# 2. The MCP SDK (the project's only third-party Python dep)
pip install --user -r /opt/claude-video/requirements.txt
# Pins `mcp>=1.20,<2.0`. Validated against mcp==1.29.0 + pydantic 2.10.

# 3. (Optional) Whisper API key — only needed when a video has no captions.
#    Free captions cover most public videos; Whisper is the fallback.
mkdir -p ~/.config/watch
umask 077
cat > ~/.config/watch/.env <<'EOF'
GROQ_API_KEY=...        # preferred — cheaper & faster
# OPENAI_API_KEY=...     # alternative if you don't have Groq
EOF

# 4. Smoke test (stdio — spawn the script directly)
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | python3 /opt/claude-video/skills/watch/scripts/mcp_server.py
# Expect on stdout: JSON-RPC `result` with serverInfo.name=watch and capabilities.
# Expect on stderr: stdlib logging only. Exit code 0.
```

## Path resolution — why these paths and not others

| Path | Why this one |
|------|--------------|
| `/opt/claude-video/skills/watch/scripts/mcp_server.py` | The self-contained skill folder is the unit of distribution (`npx skills add bradautomates/claude-video -g` installs the same script). `SKILL.md` resolves its sibling `scripts/` dir via `Path(__file__).resolve().parent`; do not move the script out of `skills/watch/scripts/` or sibling imports like `from config import …` will fail. |
| `python3` (system) | The script's third-party dep is just the `mcp` SDK. After `pip install --user -r requirements.txt`, system `python3` finds it via user-site-packages. If you prefer a venv, swap the `command` in the snippet to `/absolute/path/to/.venv/bin/python` and add the same absolute path as the first `args` entry. |
| `~/.config/watch/.env` (mode `0600`) | The script reads API keys from here — never commit real keys. claude-video's own CLAUDE.md pins this path. |

## Servers That Must Be Running

| Server | Required? | Why |
|--------|-----------|-----|
| claude-video (stdio) | yes | This snippet wires it in. No daemon — Claude Code spawns it per session. |
| OpenMontage (HTTP :8900) | only when calling OM tools from a claude-video-side session | Bearer-token protected. Source token from `/opt/OpenMontage/.env` (`MCP_API_TOKEN`). |
| Voicebox (HTTP :17493) | only when calling Voicebox tools from a claude-video-side session | Header auth (`X-Voicebox-Client-Id`). |

The snippet in `integration/claude-video.mcp.json` lists all three so a
Claude Code session inside `/opt/claude-video` is one-stop; trim entries
you don't need.

## End-to-end smoke test (drives the `watch` tool)

Once the snippet is in `/opt/claude-video/.mcp.json` and Claude Code has
been restarted there, run a probe that exercises the actual tool — not
just the initialize handshake:

1. Restart Claude Code in `/opt/claude-video` (or `/mcp` → `reconnect`).
2. Paste a captioned public YouTube URL with a question, e.g.:

   ```
   /watch https://youtu.be/dQw4w9WgXcQ summarize this
   ```

3. Expect the agent to call `watch(url=…, detail=transcript)` — the
   server prints frame paths + transcript + a markdown report to the
   agent's terminal, and the agent `Read`s each frame as an image before
   answering.

For a no-key, no-network test that confirms the MCP server is alive but
skips a real download, the `initialize` smoke test in the TL;DR block
above is sufficient — it proves stdio, the `mcp` SDK import, and the
server's response shape without touching the network.

## Why no code changes were required

claude-video ships an MCP server out of the box (`skills/watch/scripts/mcp_server.py`,
539 lines for the bundled VideoLingo equivalent, but the watch one is a
sibling of `watch.py` and is registered via the same `BaseTool`-style
pattern). Adding it to `.mcp.json` is sufficient because **Claude Code
itself is the orchestrator** — it calls `claude-video.watch` and
OpenMontage tools in sequence inside a single turn.

For the deeper contract between claude-video's `RunResult` and
OpenMontage's `tools/external/claude_video.py` adapter (the
`video_id` / `frames_dir` / `vtt_path` / `transcript_segments` fields
the adapter consumes), see
`/opt/claude-video/docs/openmontage-integration-inputs.md` §1 on the
claude-video side and `/opt/OpenMontage_Voicebox/docs/claude-video-integration.md`
on the OM side.

## Reloading after config changes

Claude Code reads `.mcp.json` at startup. After editing:

1. Quit and relaunch Claude Code in the affected repo, **or**
2. Use `/mcp` slash command → `reconnect` to force a refresh in-session.

## Rotation

- **`MCP_API_TOKEN`** — edit `/opt/OpenMontage/.env` and restart the
  OpenMontage MCP server (`python mcp_server.py` on `:8900`). The
  snippet in `integration/claude-video.mcp.json` is a static copy of the
  current token; update it (or document a templating step) when rotating.
- **`GROQ_API_KEY` / `OPENAI_API_KEY`** — edit `~/.config/watch/.env`.
  No restart needed; the MCP server reads it on each `watch` call.
