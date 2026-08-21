# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## Read Order — Mandatory

**Before responding to ANY user message:**

1. [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — the complete operating guide and agent contract. Contains the routing rules (onboarding, reference-video entry, pipeline selection, "Present Both Composition Runtimes" rule, checkpoint gating). Skipping it causes the wrong first action.
2. [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — architecture, key files, conventions. Single source of truth.
3. The relevant `skills/pipelines/<pipeline>/<stage>-director.md` for whatever stage you are about to execute.

Do not improvise the production workflow. OpenMontage is pipeline-driven. All production goes through `pipeline_defs/` + stage director skills + tools discovered via the registry. No ad-hoc Python scripts that call tools directly.

## Identity

OpenMontage is an open-source, agent-orchestrated video production platform. **The AI agent IS the intelligence.** Python exists only for tools and persistence. Everything else — orchestration, creative decisions, review, stage transitions — lives in YAML manifests and Markdown skills the agent reads.

```
Agent reads pipeline manifest (YAML) → reads stage director skill (MD)
→ uses tools (Python BaseTool) → self-reviews (meta skill)
→ checkpoints (Python utility) → presents to human for approval
```

Three-layer knowledge model:

- **Layer 1 — `tools/` + `tools/tool_registry.py`** — what exists (capabilities, status, cost). Every tool subclasses `tools/base_tool.py` `BaseTool` and is auto-discovered. Never import tools ad hoc; always go through the registry.
- **Layer 2 — `skills/`** — how OpenMontage wants those tools used (project conventions, quality bars, pipeline director skills, meta skills for review/checkpoints/onboarding).
- **Layer 3 — `.agents/skills/`** — vendor / technology knowledge (Remotion, GSAP, FLUX, ElevenLabs, Kling, HyperFrames, …). Each tool's `agent_skills` field points to the right Layer 3 file. **Read Layer 3 before any generation call.**

## Quick Reference — Build / Run / Test

The Makefile is the source of truth for setup. `.python-version` pins Python 3.10.12; `.venv/` is the active env.

```bash
make setup                # one-shot install: venv + Python deps + Remotion npm + demo npm + Piper TTS + HyperFrames cache + .env
make install              # Python deps only
make install-dev          # adds pytest, httpx2, pytest-asyncio
make install-gpu          # adds torch/torchaudio/torchvision + diffusers (NVIDIA GPU)

make test                 # full pytest suite in tests/
make test-contracts       # tests/contracts/ only — pipeline manifest + artifact schema checks
make test-integration     # tests/integration/ — voicebox live-MCP roundtrip; skipped gracefully if voicebox / :8900 are down

make preflight            # dump the full tool provider menu via registry.provider_menu() — firehose; use provider_menu_summary() for human-ready output
make hyperframes-doctor   # runtime check: node/ffmpeg/npx + `hyperframes doctor`
make hyperframes-warm     # refresh HyperFrames npx cache to latest

make demo                 # render zero-key demo videos (Remotion only, no API keys needed)
make demo-list            # list available demos
make lint                 # py_compile spot-check on core modules
make clean                # remove __pycache__ and .pyc files (preserves venv)
```

Target a single test file: `python -m pytest tests/path/test_foo.py -v`. No `pytest`/`unittest` global runs — always target a file.

Quick smoke snippets (bypass Makefile):

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

## Project Layout (one-liner per area)

- `tools/` — Python `BaseTool` subclasses, one file per capability (`tools/audio/`, `tools/video/`, `tools/tts/`, `tools/analysis/`, `tools/enhancement/`, etc.). All tools call via `.execute(params)` returning `ToolResult`. Discovery flows through `tools/tool_registry.py` — never hardcode tool lists.
- `pipeline_defs/` — declarative YAML manifests, one per pipeline (12 production pipelines listed in `PROJECT_CONTEXT.md`).
- `skills/pipelines/<pipeline>/<stage>-director.md` — the **HOW** for each stage. Read the director skill before executing any stage.
- `skills/meta/` — cross-cutting meta skills: `reviewer.md`, `checkpoint-protocol.md`, `onboarding.md`, `bespoke-composition.md`, `taste-direction.md`, `animation-runtime-selector.md`, `video-reference-analyst.md`, `voice-performance-director.md`.
- `skills/core/` — Layer-2 hub skills (e.g. `hyperframes.md` — when to pick HyperFrames vs Remotion).
- `.agents/skills/` — Layer 3 vendor knowledge (HyperFrames, ElevenLabs, Remotion, FLUX, Kling, Manim, …). Every generation tool exposes its Layer-3 pointer via the `agent_skills` field — read that before calling the tool.
- `schemas/` — JSON schemas: `schemas/artifacts/` (canonical artifacts), `schemas/pipelines/`, `schemas/styles/`, `schemas/tools/`, `schemas/checkpoints/`.
- `styles/*.yaml` — visual playbooks (`clean-professional`, `premium-minimalist`, `flat-motion-graphics`, `minimalist-diagram`, `ink-sketch`, `anime-ghibli`). Schema: `schemas/styles/playbook.schema.json`.
- `lib/` — persistence helpers: `checkpoint.py`, `pipeline_loader.py`, `media_profiles.py`, `config_model.py`, `hyperframes_style_bridge.py`.
- `remotion-composer/` — the React/Remotion scene stack. `SCENE_TYPES.md` lists the stock `cut.type` catalog.
- `projects/` — gitignored. Each production run creates `projects/<project-id>/` with `artifacts/`, `assets/{images,video,audio,music}/`, `renders/final.mp4`. **All tool outputs must write under here — never to the repo root.**
- `mcp_server.py` — the FastMCP server. Start with `start_mcp_server.sh`.
- `ink-theater/` — hand-drawn doodle engine + Ink Puppet mocap (`skills/creative/ink-theater.md`).
- `backlot/` — the local storyboard server (`python -m backlot open <project-id>`). The agent's only board duty is to open it; the board derives everything else from disk.

## Project Workspace Convention

Every production run creates `projects/<project-id>/` (gitignored):

```
projects/<project-id>/
├── artifacts/         # JSON artifacts from each stage (brief, script, scene_plan, …)
├── assets/{images,video,audio,music}/  # generated media + final mix
└── checkpoint_<stage>.json   # stage checkpoint (the Backlot board watches it)
└── renders/final.mp4  # final deliverable
```

Naming: kebab-case derived from the video title (e.g. `hidden-math-of-nature`).

At pipeline initialization: `python -c "from lib.checkpoint import init_project; init_project('<id>', title='<Title>', pipeline_type='<pipeline>')"`, then `python -m backlot open <project-id>`. Tools must always write under `projects/<project-id>/` — writing to the repo root, cwd, or `/tmp` is invisible to Backlot and violates the workspace contract.

## Core Invariants

Violating any of these is a defect:

1. **All production goes through a pipeline.** No ad-hoc Python scripts that call tools directly. Match the request to a `pipeline_defs/*.yaml` manifest; read its stages; read the stage director skill before executing each stage.
2. **Read Layer 3 before any generation tool call.** The tool's `agent_skills` field points to the right file. Generic prompts produce generic output.
3. **`render_runtime` is locked at proposal.** Never silently swap Remotion ↔ HyperFrames ↔ FFmpeg. If the chosen runtime is unavailable, surface a blocker and log a `render_runtime_selection` decision — do not substitute. **Present Both Composition Runtimes (HARD RULE)**: when both are available, the agent must present both options with tradeoffs and a recommendation to the user before locking.
4. **Gated stages need `human_approved=True` in the checkpoint.** `lib/checkpoint.py` enforces this; bypassing it raises a GATE VIOLATION. The pipeline manifest's `human_approval_default` is binding — never re-judge it.
5. **Tool outputs go under `projects/<project-id>/`.** Specifying a path outside `projects/` is invisible to the Backlot board.
6. **The `decision_log` is append-only.** When a previously-logged choice changes mid-run, append a new entry with the **same `(category, subject)` pair** — never silently mutate the old one or reword the subject (a reworded subject reads as a different decision and both will show).

## Pipelines at a Glance

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