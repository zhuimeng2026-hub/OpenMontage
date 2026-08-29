# OpenMontage

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

**MANDATORY: Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) before responding to ANY user message.** It contains routing rules that determine your first action based on what the user asked (onboarding, reference-video analysis, pipeline selection, the "Present Both Composition Runtimes" rule, checkpoint gating, etc.). Skipping it will cause you to take the wrong action.

Architecture, key files, tool inventory, and conventions live in [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).

There are no creative-decision rules in this file — those are in `AGENT_GUIDE.md` and `skills/meta/`. This file only adds **quick-reference build/test commands** below so future Claude instances don't have to re-read the Makefile every session.

## Quick Reference

OpenMontage is an instruction-driven, agent-first video production system. The agent itself is the orchestrator — Python is only tools and persistence. The Makefile is the source of truth for setup; targets commonly needed during development:

```bash
make setup                # one-shot install: venv + Python deps + Remotion npm + demo npm + Piper TTS + HyperFrames cache + .env
make install              # Python deps only
make install-dev          # adds pytest, httpx2, pytest-asyncio
make install-gpu          # adds torch/torchaudio/torchvision + diffusers (NVIDIA GPU)

make test                 # full pytest suite in tests/
make test-contracts       # tests/contracts/ only — pipeline manifest + artifact schema checks

make preflight            # dump the tool provider menu (configured/available counts per capability)
make hyperframes-doctor   # runtime check: node/ffmpeg/npx + `hyperframes doctor`
make hyperframes-warm     # refresh HyperFrames npx cache to latest

make demo                 # render zero-key demo videos (Remotion only, no API keys needed)
make demo-list            # list available demos
make lint                 # py_compile spot-check on core modules
make clean                # remove __pycache__ and .pyc files (preserves venv)
```

Quick smoke snippets that bypass the Makefile:

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
```

Python 3.10+ is required. The Makefile prefers `uv` if present, otherwise falls back to `python -m venv`. The active `.venv` is at `./.venv`.

## Project Layout (one-liner per area)

- `tools/` — Python `BaseTool` subclasses, one file per capability (`tools/audio/`, `tools/video/`, `tools/tts/`, `tools/analysis/`, `tools/enhancement/`, etc.). All tools call via `.execute(params)` returning `ToolResult`. Discovery flows through `tools/tool_registry.py` — never hardcode tool lists.
- `pipeline_defs/` — declarative YAML manifests, one per pipeline (12 production pipelines listed in `PROJECT_CONTEXT.md`).
- `skills/pipelines/<pipeline>/<stage>-director.md` — the **HOW** for each stage. Read the director skill before executing any stage.
- `skills/meta/` — cross-cutting meta skills: `reviewer.md`, `checkpoint-protocol.md`, `onboarding.md`, `bespoke-composition.md`, `taste-direction.md`, `animation-runtime-selector.md`, etc.
- `skills/core/` — Layer-2 hub skills (e.g. `hyperframes.md` — when to pick HyperFrames vs Remotion).
- `.agents/skills/` — Layer 3 vendor knowledge (HyperFrames, ElevenLabs, Remotion, FLUX, Kling, Manim, etc.). Every generation tool exposes its Layer-3 pointer via the `agent_skills` field — read that before calling the tool.
- `schemas/` — JSON schemas: `schemas/artifacts/` (canonical artifacts), `schemas/pipelines/`, `schemas/styles/`, `schemas/tools/`, `schemas/checkpoints/`.
- `styles/*.yaml` — visual playbooks (`clean-professional`, `premium-minimalist`, `flat-motion-graphics`, `minimalist-diagram`, `ink-sketch`). Schema: `schemas/styles/playbook.schema.json`.
- `lib/` — persistence helpers: `checkpoint.py`, `pipeline_loader.py`, `media_profiles.py`, `config_model.py`, `hyperframes_style_bridge.py`.
- `remotion-composer/` — the React/Remotion scene stack. `SCENE_TYPES.md` lists the stock `cut.type` catalog.
- `projects/` — gitignored. Each production run creates `projects/<project-id>/` with `artifacts/`, `assets/{images,video,audio,music}/`, `renders/final.mp4`. **All tool outputs must write under here — never to the repo root.** Outputs that have no project (smoke-test TTS, ad-hoc renders, debug dumps) belong in `projects/_scratch/<category>/` — see `projects/_scratch/README.md`.
- `mcp_server.py` — the FastMCP server. Start with `start_mcp_server.sh`.
- `ink-theater/` — hand-drawn doodle engine + Ink Puppet mocap (`skills/creative/ink-theater.md`).
- `backlot/` — the local storyboard server (`python -m backlot open <project-id>`). The agent's only board duty is to open it; the board derives everything else from disk.

## Core Invariants

The agent contract — violating any of these is a defect:

1. **All production goes through a pipeline.** No ad-hoc Python scripts that call tools directly. Match the request to a `pipeline_defs/*.yaml` manifest; read its stages; read the stage director skill before executing each stage.
2. **Read Layer 3 before any generation tool call.** The tool's `agent_skills` field points to the right file. Generic prompts produce generic output.
3. **`render_runtime` is locked at proposal.** Never silently swap Remotion ↔ HyperFrames ↔ FFmpeg. If the chosen runtime is unavailable, surface a blocker and log a `render_runtime_selection` decision — do not substitute.
4. **Gated stages need `human_approved=True` in the checkpoint.** `lib/checkpoint.py` enforces this; bypassing it raises a GATE VIOLATION. The pipeline manifest's `human_approval_default` is binding — never re-judge it.
5. **Tool outputs go under `projects/<project-id>/`.** Specifying a path outside projects/ is invisible to the Backlot board and violates the workspace contract. Outputs with no real project (smoke-test TTS, ad-hoc renders, debug dumps) go to `projects/_scratch/<category>/` instead — never to the repo root.
6. **The `decision_log` is append-only.** When a previously-logged choice changes mid-run, append a new entry with the **same `(category, subject)` pair** — never silently mutate the old one. A reworded subject reads as a different decision.

`AGENT_GUIDE.md` and `PROJECT_CONTEXT.md` are the authoritative sources. When in doubt, read them over this file.
