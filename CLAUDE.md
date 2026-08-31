# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read Order — Mandatory

Before responding to ANY user message:

1. [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — complete operating guide and agent contract. Contains the routing rules (onboarding, reference-video entry, pipeline selection, "Present Both Composition Runtimes" rule, checkpoint gating). Skipping it causes the wrong first action.
2. [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — architecture, key files, conventions. Single source of truth.
3. `skills/pipelines/<pipeline>/<stage>-director.md` for whatever stage you are about to execute.

`AGENTS.md`, `CURSOR.md`, `COPILOT.md`, `CODEX.md`, `.cursor/rules/openmontage.mdc` (Cursor: `alwaysApply: true`, `globs: ["**/*"]`), and `.github/copilot-instructions.md` are all thin pointers to the two files above. Do not duplicate content between them.

## Identity

OpenMontage is an open-source, agent-orchestrated video production platform. **The AI agent IS the intelligence.** Python exists only for tools and persistence. Everything else — orchestration, creative decisions, review, stage transitions — lives in YAML manifests and Markdown skills the agent reads.

```
Agent reads pipeline manifest (YAML) → reads stage director skill (MD)
→ uses tools (Python BaseTool) → self-reviews (meta skill)
→ checkpoints (Python utility) → presents to human for approval
```

The Makefile is the source of truth for setup. `.python-version` pins Python 3.10.12; `.venv/` is the active env. Below are the commands you'll reach for constantly — for the full reference, see the Makefile itself.

```bash
make setup                # one-shot install: venv + Python deps + Remotion npm + demo npm + Piper TTS + HyperFrames cache + .env
make install              # Python deps only
make install-dev          # adds pytest, httpx, pytest-asyncio
make install-gpu          # adds torch/torchaudio/torchvision + diffusers (NVIDIA GPU)

make test                 # full pytest suite in tests/
make test-contracts       # tests/contracts/ only — pipeline manifest + artifact schema checks
make test-integration     # tests/integration/ — voicebox live-MCP roundtrip; skipped gracefully if voicebox / :8900 are down

make preflight            # dump the full tool provider menu via registry.provider_menu() — firehose; use provider_menu_summary() for human-ready output
make hyperframes-doctor   # runtime check: node/ffmpeg/npx + `hyperframes doctor`
make hyperframes-warm     # refresh the HyperFrames npx cache to latest (re-fetch the npm package)
make musicgen-fetch       # pre-download MusicGen-small weights (~300MB) so music_gen_local works offline

make demo                 # render zero-key demo videos (Remotion only, no API keys needed)
make demo-list            # list available demos
make tweak-server         # start the tweak sidecar (FastAPI on :8901; talks to MCP at :8900)
make tweak-server-stop    # stop the tweak sidecar
make lint                 # py_compile spot-check on core modules
make clean                # remove __pycache__ and .pyc files (preserves venv)
```

Target a single test file: `python -m pytest tests/path/test_foo.py -v`. No `pytest`/`unittest` global runs — always target a file.

Quick smoke snippets (bypass the Makefile):

```bash
# Capability snapshot (human-ready)
python -c "from tools.tool_registry import registry; \
import json; registry.discover(); \
print(json.dumps(registry.provider_menu_summary(), indent=2))"

# Which render engines are installed?
python -c "from tools.tool_registry import registry; registry.discover(); \
print(registry._tools['video_compose'].get_info().get('render_engines'))"

# Initialize a project workspace (called by the agent at start of every run)
python -c "from lib.checkpoint import init_project; \
init_project('<project-id>', title='<Title>', pipeline_type='animated-explainer')"

# Open the Backlot live storyboard for a project
python -m backlot open <project-id>
```

For a production run, also learn `python -m backlot open <project-id>` — it starts a local board that watches `projects/<id>/` and surfaces stages, scene-by-scene filmstrip, decision log, and cost in real time. Backlot is **read-only observation**; it never blocks the pipeline.

### Test layout

`tests/` is organized by purpose, not by file:

- `tests/contracts/` — pipeline manifests + artifact schema checks (always green in CI).
- `tests/integration/` — voicebox / `:8900` live-MCP roundtrip; **skipped gracefully** if those are down (so `make test` stays green). Override `VOICEBOX_TEST_TTS_TIMEOUT_S` for cold voicebox installs.
- `tests/backlot/` — board state derivation (file → JSON contract).
- `tests/qa/` — runtime output validation scripts (look at `QA_PLAN.md` before adding).
- `tests/regression/` — historical bug regressions; add a test here when you fix a reproducible bug.
- top-level `test_*.py` — governance / governance-adjacent tests (session assets, share links, MCP HTTP keep-alive, etc.).

Target a single file: `python -m pytest tests/<area>/test_foo.py -v`. No global `pytest` runs — they pull in integration tests that need live services.

## Runtime Entry Points

| Surface | Command | Port (default) | Purpose |
|---|---|---|---|
| **MCP server** | `python mcp_server.py` (or `./start_mcp_server.sh`) | `:8900` | The agent-facing tool surface. Bearer-token auth via `MCP_API_TOKEN`. Port overridable via `MCP_PORT`. See [`MCP_SERVER.md`](MCP_SERVER.md). |
| **Tweak sidecar** | `make tweak-server` | `:8901` | End-user render-tweak UI that talks to MCP. See [`docs/tweak-server.md`](docs/tweak-server.md). |
| **Backlot board** | `python -m backlot open <project-id>` | `:8900` (auto-derived) | Live, read-only storyboard derived from disk. |

Start the MCP server before any production run that goes through it; tweak-server and Backlot are optional but recommended.

### Single-port production topology (post 2026-08-31)

In the deployed platform the public-facing port is **`:8900`** — but it now fronts the **vclaw** Control Plane, not the OpenMontage MCP. The Python MCP runs on `:8902` behind vclaw (which proxies `/mcp` and `/api/mcp/proxy`). The tweak-sidecar still binds `:8901`. When running `mcp_server.py` standalone (no vclaw front), set `MCP_PORT=:8900` to match the default and use Bearer `MCP_API_TOKEN`.

See [`docs/single-port-arch.md`](docs/single-port-arch.md) for the architecture rationale, SSE streaming fix, and auth split (raw Bearer vs JWT-with-`mcp:use`).

## Layering — Picking the Right Entry for an External Caller

Three entry points exist, each for a different kind of caller. Use the wrong one and you get an awkward fit (e.g. wrapping an agent in a browser-shaped proxy, or calling a business API with raw JSON-RPC).

| External caller | Entry | Why |
|---|---|---|
| **Raw MCP client** (Claude Code, Cursor, custom JSON-RPC client) | `mcp_server.py :8900/mcp` with `Authorization: Bearer MCP_API_TOKEN` | The protocol surface. No business layer in the way. |
| **OpenClaw-style agent** (Node/TS, Python, Go — anything that wants WeChat-小程序 tenancy + billing + audit + job queue) | **`/opt/vclaw/` Control Plane `:8080`** + 8-verb Agent Gateway | Business logic + tenant isolation + quota + durable job queue + signed-URL issuance. vclaw translates business verbs into OpenMontage MCP calls under the hood. |
| **FrameFlow browser SPA only** | `frameflow/bff/ :8080` | SPA-specific: hides `MCP_API_TOKEN`, manages per-user MCP session affinity (so chunked uploads + final render land on the same `Mcp-Session-Id`), handles WeChat `snsapi` web login. **Not for agents.** |

**Don't** route an OpenClaw-style agent through `frameflow/bff/`. The BFF's per-user session affinity, `ff_sid` cookie, and web-login flow are SPA-shaped — an agent running server-side has none of those constraints and shouldn't pay the cost.

For the full integration recipe (config, auth tokens, verb contracts, render + preview flows), see the vclaw docs:

- `/opt/vclaw/docs/openclaw-integration.md` — layering, config, auth, where to start
- `/opt/vclaw/docs/render-flow.md` — 4-level render pipeline (storyboard → animatic → sample → render) through the gateway verbs
- `/opt/vclaw/docs/preview-flow.md` — preview polling + approval gate

The OpenClaw-runtime side lives in `/opt/vclaw/openclaw/solutions/product-video-production/` (`mcp/control-plane-gateway.mjs` is the stdio MCP bridge that calls back into vclaw).

## Core Invariants — Violating Any of These Is a Defect

1. **All production goes through a pipeline.** No ad-hoc Python scripts that call tools directly. Match the request to a `pipeline_defs/*.yaml` manifest; read its stages; read the stage director skill before executing each stage.
2. **Read Layer 3 before any generation tool call.** The tool's `agent_skills` field points to the right file. Generic prompts produce generic output.
3. **`render_runtime` is locked at proposal.** Never silently swap Remotion ↔ HyperFrames ↔ FFmpeg. If the chosen runtime is unavailable, surface a blocker and log a `render_runtime_selection` decision — do not substitute. When both runtimes are available, **Present Both Composition Runtimes (HARD RULE)** — present both with tradeoffs and a recommendation, then wait for explicit approval.
4. **Gated stages need `human_approved=True` in the checkpoint.** `lib/checkpoint.py` enforces this; bypassing it raises a GATE VIOLATION. The pipeline manifest's `human_approval_default` is binding — never re-judge it.
5. **Tool outputs go under `projects/<project-id>/`.** Specifying a path outside `projects/` is invisible to the Backlot board and violates the workspace contract. Outputs with no real project (smoke-test TTS, ad-hoc renders, debug dumps) go to `projects/_scratch/<category>/` instead — never to the repo root.
6. **The `decision_log` is append-only.** When a previously-logged choice changes mid-run, append a new entry with the **same `(category, subject)` pair** — never silently mutate the old one or reword the subject. The board keys decisions on the pair, and a reworded subject reads as a different decision.

`AGENT_GUIDE.md` and `PROJECT_CONTEXT.md` are the authoritative sources. When in doubt, read them over this file.

## Pipelines — Pointer

The full roster with stability notes lives in `AGENT_GUIDE.md` § "Available Pipelines" and `PROJECT_CONTEXT.md` § "Available Pipelines". 13 pipelines; `video-template-remix` is the default.

## Three-Layer Knowledge Model — Pointer

See `PROJECT_CONTEXT.md` § "Knowledge Architecture" for the canonical description. Quick version: Layer 1 = `tools/` (what exists), Layer 2 = `skills/` (how OpenMontage uses it), Layer 3 = `.agents/skills/` (vendor/tech knowledge). Each tool's `agent_skills` field bridges 1 → 3.

## Project Layout — Pointer

See `PROJECT_CONTEXT.md` § "Key Files" for the authoritative one-liner-per-area map. At a glance: `tools/` (BaseTool subclasses, auto-discovered), `pipeline_defs/` (YAML manifests), `skills/pipelines/<pipeline>/` (stage director skills), `skills/meta/` (cross-cutting meta skills), `.agents/skills/` (Layer 3), `schemas/` (JSON schemas), `styles/*.yaml` (visual playbooks), `lib/` (persistence helpers), `remotion-composer/` (React/Remotion scene stack), `projects/` (gitignored, one dir per production run), `sources/` (gitignored binaries), `mcp_server.py` (FastMCP server), `backlot/` (live storyboard server).

## Custom Script Contract (Remotion `create_remotion_video_share`)

"Script mode" lets users submit custom Remotion TSX source (e.g. generated by DeepSeek), compiled and rendered at runtime by `remotion-composer/src/CustomComposition.tsx`. When generating or authoring a script, follow this props contract exactly — these are the only inputs the component injects into your code:

| prop | type | meaning | notes |
|---|---|---|---|
| `images` | `string[]` | uploaded image relative paths | must be referenced with `staticFile(src)`; staged under `public/_staged/<id>/` |
| `durationPerImage` | `number` | seconds per image | default `3`; set by the `duration` arg of `create_remotion_video_share` |
| `fps` | `number` | composition frame rate | fixed at `30` |
| `width` / `height` | `number` | canvas dimensions | e.g. `1080 × 1920` for 9:16, decided by `aspect_ratio` |

**Rules:**

- Export a renderable component: `export const MyComposition = (props) => {...}` or `export default`.
- The component MUST return a React element using Remotion APIs (`AbsoluteFill`, `useCurrentFrame`, `Sequence`, ...).
- Image paths are relative to `public/` and MUST be wrapped with `staticFile(src)` — no absolute paths or `file://`.
- Total duration (frames) = `images.length × durationPerImage × fps`, computed by `Root.tsx`'s `calculateCustomMetadata`; user code cannot change total length.
- The default template is `DEFAULT_COMP` in the BFF `web/index.html`; script generators (e.g. DeepSeek) should align to it.

Minimal example:

```tsx
import {AbsoluteFill, useCurrentFrame, Sequence, staticFile} from "remotion";

export const MyComposition = ({images, durationPerImage = 3, fps = 30}) => {
  const frame = useCurrentFrame();
  const fpi = Math.round(durationPerImage * fps);
  const idx = Math.min(Math.floor(frame / fpi), Math.max(images.length, 1) - 1);
  return (
    <AbsoluteFill>
      {images.map((src, i) => (
        <Sequence key={i} from={i * fpi} durationInFrames={fpi}>
          <AbsoluteFill>
            <img src={staticFile(src)} style={{width: "100%", height: "100%", objectFit: "cover"}} />
          </AbsoluteFill>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
```

## Deep-Dive Docs (When You Need More)

- `README.md` — project pitch, prompt gallery, pipeline overview, sponsors
- `docs/PROVIDERS.md` — every provider with setup, pricing, and free-tier notes
- `docs/PR_REVIEW_GUIDE.md` — review checklist for landing changes
- `docs/ARCHITECTURE.md` — full technical reference (decision log, schema internals)
- `docs/tweak-server.md` — end-user render-tweak sidecar protocol
- `MCP_SERVER.md` — MCP tool surface and request/response contract
- `skills/INDEX.md` — full Layer-2 skill index (which skill for which job)
- `backlot/README.md` — how the live storyboard derives state from disk

## Session-local Claude resources (`.claude/`)

The repo has its own Claude-side state under `.claude/`:

- `commands/` — custom slash commands available to Claude Code in this repo.
- `skills/` — session-scoped Layer 2 skills layered on top of the repo's `skills/`.
- `worktrees/` — auto-managed isolated worktrees.
- `scheduled_tasks.lock` — locks held by autonomous cron/loop tasks.

Do not edit `.claude/` directly from Python — Claude Code owns that subtree.