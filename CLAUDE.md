# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## Read Order — Mandatory

Before responding to ANY user message:

1. [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — complete operating guide and agent contract. Contains the routing rules (onboarding, reference-video entry, pipeline selection, "Present Both Composition Runtimes" rule, checkpoint gating). Skipping it causes the wrong first action.
2. [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — architecture, key files, conventions. Single source of truth.
3. `skills/pipelines/<pipeline>/<stage>-director.md` for whatever stage you are about to execute.

`AGENTS.md`, `CURSOR.md`, `COPILOT.md`, `CODEX.md`, `.cursor/rules/openmontage.mdc`, `.github/copilot-instructions.md`, and this file are all thin pointers to the two files above. Do not duplicate content between them.

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

## Three-Layer Knowledge Model

This is the single most important architectural insight. Internalize it before touching anything else.

- **Layer 1 — `tools/` + `tools/tool_registry.py`** — what exists (capabilities, status, cost). Every tool subclasses `tools/base_tool.py` `BaseTool` and is auto-discovered. Never import tools ad hoc; always go through the registry.
- **Layer 2 — `skills/`** — how OpenMontage wants those tools used (project conventions, quality bars, pipeline director skills, meta skills for review/checkpoints/onboarding).
- **Layer 3 — `.agents/skills/`** — vendor / technology knowledge (Remotion, GSAP, FLUX, ElevenLabs, Kling, HyperFrames, …). Each tool's `agent_skills` field points to the right Layer 3 file. **Read Layer 3 before any generation call.**

## Core Invariants — Violating Any of These Is a Defect

1. **All production goes through a pipeline.** No ad-hoc Python scripts that call tools directly. Match the request to a `pipeline_defs/*.yaml` manifest; read its stages; read the stage director skill before executing each stage.
2. **Read Layer 3 before any generation tool call.** The tool's `agent_skills` field points to the right file. Generic prompts produce generic output.
3. **`render_runtime` is locked at proposal.** Never silently swap Remotion ↔ HyperFrames ↔ FFmpeg. If the chosen runtime is unavailable, surface a blocker and log a `render_runtime_selection` decision — do not substitute. When both runtimes are available, **Present Both Composition Runtimes (HARD RULE)** — present both with tradeoffs and a recommendation, then wait for explicit approval.
4. **Gated stages need `human_approved=True` in the checkpoint.** `lib/checkpoint.py` enforces this; bypassing it raises a GATE VIOLATION. The pipeline manifest's `human_approval_default` is binding — never re-judge it.
5. **Tool outputs go under `projects/<project-id>/`.** The Backlot board (live storyboard: `python -m backlot open <project-id>`) only watches that directory. Outputs with no real project (smoke-test TTS, ad-hoc renders, debug dumps) go to `projects/_scratch/<category>/` instead — never to the repo root.
6. **The `decision_log` is append-only.** When a previously-logged choice changes mid-run, append a new entry with the **same `(category, subject)` pair** — never silently mutate the old one or reword the subject. The board keys decisions on the pair.

## Project Layout (one-liner per area)

- `tools/` — Python `BaseTool` subclasses, one file per capability (`tools/audio/`, `tools/video/`, `tools/tts/`, `tools/analysis/`, `tools/enhancement/`, …). All tools call via `.execute(params)` returning `ToolResult`. Discovery flows through `tools/tool_registry.py` — never hardcode tool lists.
- `pipeline_defs/` — declarative YAML manifests, one per pipeline (12 production pipelines listed in `PROJECT_CONTEXT.md`).
- `skills/pipelines/<pipeline>/<stage>-director.md` — the **HOW** for each stage. Read the director skill before executing any stage.
- `skills/meta/` — cross-cutting meta skills: `reviewer.md`, `checkpoint-protocol.md`, `onboarding.md`, `bespoke-composition.md`, `taste-direction.md`, `animation-runtime-selector.md`, `video-reference-analyst.md`, `voice-performance-director.md`.
- `skills/core/` — Layer-2 hub skills (e.g. `hyperframes.md` — when to pick HyperFrames vs Remotion).
- `.agents/skills/` — Layer 3 vendor knowledge (HyperFrames, ElevenLabs, Remotion, FLUX, Kling, Manim, …). Every generation tool exposes its Layer-3 pointer via the `agent_skills` field — read that before calling the tool.
- `schemas/` — JSON schemas: `schemas/artifacts/` (canonical artifacts), `schemas/pipelines/`, `schemas/styles/`, `schemas/tools/`, `schemas/checkpoints/`.
- `styles/*.yaml` — visual playbooks (`clean-professional`, `premium-minimalist`, `flat-motion-graphics`, `minimalist-diagram`, `ink-sketch`, `anime-ghibli`). Schema: `schemas/styles/playbook.schema.json`.
- `lib/` — persistence helpers: `checkpoint.py`, `pipeline_loader.py`, `media_profiles.py`, `config_model.py`, `hyperframes_style_bridge.py`.
- `remotion-composer/` — the React/Remotion scene stack. `SCENE_TYPES.md` lists the stock `cut.type` catalog.
- `projects/` — gitignored. Each production run creates `projects/<project-id>/` with `artifacts/`, `assets/{images,video,audio,music}/`, `renders/final.mp4`. **All tool outputs must write under here — never to the repo root.** Outputs that have no project (smoke-test TTS, ad-hoc renders, debug dumps) belong in `projects/_scratch/<category>/` — see `projects/_scratch/README.md`.
- `sources/` — gitignored binaries (`.gitignore` excludes `*.wav`, `*.mp4`, `*.mov`). Holds reference inputs that drive `video-reference-analyst` and similar skills: scene annotations, transcripts, and the source media itself. Keep the `.json` annotations tracked; the binaries stay local.
- `mcp_server.py` — the FastMCP server. Start with `./start_mcp_server.sh` (cross-platform: locks npm install to `package-lock.json`, neutralizes WorkBuddy safe-delete shim on Windows, pre-pulls headless Chrome).
- `ink-theater/` — hand-drawn doodle engine + Ink Puppet mocap (`skills/creative/ink-theater.md`).
- `backlot/` — the local storyboard server (`python -m backlot open <project-id>`). The agent's only board duty is to open it; the board derives everything else from disk.

## Project Workspace

Every production run creates `projects/<project-id>/` (gitignored) with `artifacts/`, `assets/{images,video,audio,music}/`, and `renders/final.mp4`. Initialize with `python -c "from lib.checkpoint import init_project; init_project('<id>', title='<Title>', pipeline_type='<pipeline>')"`, then open the board with `python -m backlot open <project-id>`. Tools writing to the repo root, cwd, or `/tmp` are invisible to Backlot — that violates the workspace contract.

**Exception — `projects/_scratch/`**: when a tool output genuinely has no project (smoke-testing a TTS provider, an ad-hoc ffmpeg probe, a one-off render), land it in `projects/_scratch/<category>/` instead. This keeps the workspace contract enforceable (nothing escapes `projects/`) while giving agents an honest place to put throwaway output. See `projects/_scratch/README.md` for the category list. Backlot does **not** watch this directory — by design.

## Pipelines — At a Glance

| Pipeline | Best For |
|---|---|
| `animated-explainer` | Topic → fully generated explainer (production) |
| `talking-head` | Footage-led speaker videos (beta) |
| `screen-demo` | Screen recordings + walkthroughs (production) |
| `clip-factory` | Many clips from one long source (beta) |
| `podcast-repurpose` | Podcast highlights and derivatives (beta) |
| `cinematic` | Trailer / teaser / mood-led edits (production) |
| `animation` | Motion-graphics + animation-first (production) |
| `character-animation` | Local rigged cartoon characters + reusable acting (beta) |
| `hybrid` | Source footage + support visuals (production) |
| `avatar-spokesperson` | Presenter-led avatar / lip-sync (production) |
| `localization-dub` | Subtitle / dub / translated variants (beta) |
| `documentary-montage` | Real-footage edit from free/open archives (no paid video APIs) |
| `framework-smoke` | Test: minimal 2-stage smoke test (test) |

`AGENT_GUIDE.md` and `PROJECT_CONTEXT.md` are the authoritative sources. When in doubt, read them over this file.

## Deep-Dive Docs (When You Need More)

- `README.md` — project pitch, prompt gallery, pipeline overview, sponsors
- `docs/PROVIDERS.md` — every provider with setup, pricing, and free-tier notes
- `docs/PR_REVIEW_GUIDE.md` — review checklist for landing changes
- `docs/ARCHITECTURE.md` — full technical reference (decision log, schema internals)
- `docs/tweak-server.md` — end-user render-tweak sidecar protocol
- `MCP_SERVER.md` — MCP tool surface and request/response contract
- `skills/INDEX.md` — full Layer-2 skill index (which skill for which job)
- `backlot/README.md` — how the live storyboard derives state from disk
