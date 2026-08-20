# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read the agent guide first

**MANDATORY: Read [`AGENT_GUIDE.md`](AGENT_GUIDE.md) before responding to any user message.** It contains routing rules that determine your first action based on what the user asked — onboarding vs. reference-video vs. pipeline production. Skipping it will cause the wrong action.

For the full architecture, key files, and conventions, see [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

> All platform-specific agent files (`CLAUDE.md`, `CODEX.md`, `CURSOR.md`, `COPILOT.md`, `.cursor/rules/openmontage.mdc`, `.github/copilot-instructions.md`, `.windsurfrules`) point back to `AGENT_GUIDE.md`. Edit the guides, not the pointers.

## What this project is

OpenMontage is an **agent-orchestrated video production platform**. There is no Python orchestrator. The LLM coding assistant IS the control plane — it reads pipeline manifests, follows stage director skills, calls Python tools, and checkpoints state.

```
Agent reads pipeline manifest (YAML) → reads stage director skill (MD)
→ uses tools (Python BaseTool) → self-reviews (meta skill)
→ checkpoints (Python utility) → presents to human for approval
```

Python provides tools and persistence only. Creative decisions, review criteria, and pipeline logic live in Markdown skills and YAML manifests.

## Common commands

```bash
make setup                # one-command install: venv, requirements, Remotion composer, Piper TTS, HyperFrames cache, .env
make install              # pip install -r requirements.txt only
make install-dev          # adds dev tools (pytest, etc.)
make install-gpu          # local video/image model deps (torch, diffusers, etc.)
make test                 # full pytest run
make test-contracts       # contract tests under tests/contracts/ (no API keys)
make preflight            # dumps the capability provider menu — what tools are usable right now
make hyperframes-doctor   # validates the HyperFrames runtime (node/ffmpeg/npx + hyperframes doctor)
make hyperframes-warm     # refresh the npx hyperframes cache
make demo                 # render zero-key demo videos (Remotion only)
make demo-list            # list available demos
make lint                 # py_compile check on core modules
make clean                # remove __pycache__ and .pyc outside the venv
```

Direct equivalents when not using the Makefile:

```bash
# Preflight — start here for any production request
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_menu_summary(), indent=2))"

# Provider / capability catalogs
python -c "from tools.tool_registry import registry; registry.discover(); print(json.dumps(registry.capability_catalog(), indent=2))"
python -c "from tools.tool_registry import registry; registry.discover(); print(json.dumps(registry.provider_catalog(), indent=2))"

# Run a single test
.venv/bin/python -m pytest tests/contracts/test_specific.py -v
# Or with -k for selection:
.venv/bin/python -m pytest tests/ -v -k registry
```

## High-level architecture

### Three-layer knowledge model

```
Layer 1: tools/ + pipeline_defs/   "What exists"          — executable capabilities + orchestration
Layer 2: skills/                   "How we use it"        — OpenMontage conventions, quality bars
Layer 3: .agents/skills/           "How the tech works"   — external API knowledge (vendored)
```

Each tool's `agent_skills[]` field bridges Layer 1 to Layer 3. Before calling any generation tool, the agent must read its referenced Layer 3 skill — generic prompts give generic results.

### Pipeline state machine

```
research → proposal → script → scene_plan → assets → edit → compose → publish
```

Some pipelines substitute or insert stages (e.g. `character-animation` adds `character_design` and `rig_plan`; `talking-head` and `hybrid` skip the research stage). The pipeline manifest is the authority.

### Project workspace contract

Every production creates a workspace under `projects/` (gitignored):

```
projects/<project-id>/
├── artifacts/        # canonical JSON per stage (research_brief, script, scene_plan, ...)
├── assets/{images,video,audio,music,subtitles.srt}
└── renders/final.mp4
```

Tools must write to these paths via explicit `output_path`. Writing outside `projects/` is invisible to the Backlot board and violates the workspace contract.

### Available pipelines

| Pipeline | Category | Manifest |
|----------|----------|----------|
| `animated-explainer` | generated | `pipeline_defs/animated-explainer.yaml` |
| `animation` | animation | `pipeline_defs/animation.yaml` |
| `avatar-spokesperson` | talking_head | `pipeline_defs/avatar-spokesperson.yaml` |
| `cinematic` | cinematic | `pipeline_defs/cinematic.yaml` |
| `character-animation` | animation | `pipeline_defs/character-animation.yaml` |
| `clip-factory` | custom | `pipeline_defs/clip-factory.yaml` |
| `documentary-montage` | hybrid | `pipeline_defs/documentary-montage.yaml` |
| `hybrid` | hybrid | `pipeline_defs/hybrid.yaml` |
| `localization-dub` | custom | `pipeline_defs/localization-dub.yaml` |
| `podcast-repurpose` | hybrid | `pipeline_defs/podcast-repurpose.yaml` |
| `screen-demo` | screen_recording | `pipeline_defs/screen-demo.yaml` |
| `talking-head` | talking_head | `pipeline_defs/talking-head.yaml` |
| `framework-smoke` | custom | `pipeline_defs/framework-smoke.yaml` |

### Tool registry

`tools/tool_registry.py` is a singleton (`registry`) that auto-discovers every `BaseTool` subclass via `pkgutil.walk_packages()` — no manual registration. All tool classes live in `tools/<capability>/<file>.py` and follow PascalCase naming without a `Tool` suffix (`ElevenLabsTTS`, not `ElevenLabsTTSTool`). They expose `.execute(inputs) -> ToolResult` (not `.run()`).

Selectors auto-route multi-provider capabilities (`tts_selector`, `image_selector`, `video_selector`) via the registry. Adding a new provider tool to a capability folder makes it routable through the matching selector with no selector code changes.

### Canonical artifacts (schema-validated, in `schemas/artifacts/`)

`research_brief`, `proposal_packet`, `brief`, `script`, `scene_plan`, `asset_manifest`, `edit_decisions`, `render_report`, `final_review`, `publish_log`, `decision_log`, `cost_log`, `source_media_review`, `video_analysis_brief`, plus character-animation: `character_design`, `rig_plan`, `pose_library`, `action_timeline`, `character_qa_report`. Any stage output that's a JSON artifact must validate against its schema before checkpoint.

### Composition runtimes (locked at proposal)

`video_compose` dispatches to one of three engines based on `edit_decisions.render_runtime`:

- **Remotion** — React/Node. Default for data-driven explainers, image animation, scene component stack. Subproject at `remotion-composer/`.
- **HyperFrames** — HTML/CSS/GSAP via `npx hyperframes` (Node ≥ 22). Default for motion-graphics-heavy briefs, kinetic typography, website-to-video, SVG/GSAP character rigs. Driver: `tools/video/hyperframes_compose.py`.
- **FFmpeg** — always available; handles pure concat/trim and subtitle burn-in.

**Silent runtime swaps are a CRITICAL governance violation.** If `render_runtime=hyperframes` and HyperFrames is unavailable, surface a blocker — do not silently route to Remotion or FFmpeg. See `skills/core/hyperframes.md` for the decision matrix.

### Checkpoint & human approval

Checkpoints live at `projects/<id>/checkpoint_<stage>.json`. `lib/checkpoint.py` enforces: a gated stage (`human_approval_default: true` in the manifest) cannot be written `completed` without `human_approved=True`. Superseded checkpoints archive to `projects/<id>/history/`. Agents read `human_approval_default` from the manifest — never re-judge.

## Critical rules

These are non-negotiable; violations are reviewer findings:

1. **No ad-hoc scripts for production.** All production flows through pipeline manifests + stage director skills + the tool registry. Do not write throwaway Python to call tools directly.
2. **No silent provider/runtime swaps.** Decide at proposal, log the decision (including `options_considered` and `rejected_because`), and carry `decision_log` entries forward — append a new `(category, subject)` pair when a decision changes mid-run.
3. **Read the stage director skill before each stage.** Director skills teach the agent HOW to execute that stage — quality bar, workflow, review criteria.
4. **Read the Layer 3 skill before any generation tool call.** Check the tool's `agent_skills[]` field.
5. **Never bypass preflight.** User must see the capability menu — what's configured, what's missing, what a 1-minute env var would unlock.
6. **Present both composition runtimes when both are available.** Even if the manifest recommends one, the agent must surface both options at proposal and wait for explicit approval.
7. **Do not hardcode provider names or API key names.** Read them from the registry's `install_instructions` and `dependencies` fields.
8. **Templated vs. Atelier composition mode** is orthogonal to runtime. Default to atelier for hero work; route through `skills/meta/taste-direction.md` then `skills/meta/bespoke-composition.md`.

## Key files

| Path | Purpose |
|------|---------|
| `AGENT_GUIDE.md` | Agent contract, routing rules, governance. **Read first.** |
| `PROJECT_CONTEXT.md` | Architecture reference, key files, conventions. |
| `docs/ARCHITECTURE.md` | Deep technical reference (derived from code exploration). |
| `tools/base_tool.py` | `BaseTool` contract — every tool inherits from this. |
| `tools/tool_registry.py` | Auto-discovery + `provider_menu_summary()` / `capability_catalog()` / `provider_catalog()`. |
| `tools/cost_tracker.py` | Budget governance (estimate → reserve → reconcile). |
| `lib/checkpoint.py` | Checkpoint writer/reader. Enforces approval gates. |
| `lib/pipeline_loader.py` | Pipeline manifest loader + helpers. |
| `lib/config_model.py` | Pydantic config model (loaded from `config.yaml`). |
| `lib/media_profiles.py` | Platform render profiles (YouTube, TikTok, Reels, ...). |
| `styles/playbook_loader.py` | Style playbook loader + design intelligence. |
| `pipeline_defs/*.yaml` | Pipeline manifests — the production playbooks. |
| `skills/pipelines/<pipeline>/` | Per-pipeline stage director skills. |
| `skills/meta/` | Cross-cutting meta skills (`reviewer`, `checkpoint-protocol`, ...). |
| `skills/INDEX.md` | Skill index by category + Layer 3 mapping. |
| `schemas/artifacts/*.schema.json` | Canonical artifact validation. |
| `remotion-composer/` | Remotion React subproject (npm install here). |
| `backlot/` | Living storyboard board (FastAPI). `python -m backlot open`. |
| `tests/contracts/` | Contract tests (no API keys). |

## Reference for future work

- Adding a pipeline: create YAML in `pipeline_defs/` (validate against `schemas/pipelines/pipeline_manifest.schema.json`), add stage director skills in `skills/pipelines/<name>/`, reference meta skills, list compatible playbooks, add tests in `tests/contracts/`.
- Adding a tool: place under `tools/<capability>/`, inherit from `BaseTool`, set all contract fields (`name`, `version`, `tier`, `capability`, `provider`, `runtime`, `supports`, `fallback_tools`, `agent_skills`), implement `execute()`, and add a schema in `schemas/tools/` if I/O is complex. The registry discovers it automatically.
- Adding a Layer 3 skill: drop it in `.agents/skills/` (vendored) or symlink it; record the dependency from any consuming tool's `agent_skills[]`.

## Testing

```bash
make test-contracts   # schema/contract checks — no API keys needed, fast
make test              # full pytest run under tests/
```

Per-test invocations and pytest configuration live in `tests/` (subdirs: `contracts/`, `qa/`, `eval/`, `pipelines/`, `tools/`, `styles/`, `lib/`).

## Backlot board (live storyboard)

`python -m backlot open` (or `python -m backlot open <project-id>`) starts the local board that watches `projects/` and renders live stage progress, the script, scene cards, and provider decisions. The board is an observer; a production never blocks on it. See `backlot/README.md`.
