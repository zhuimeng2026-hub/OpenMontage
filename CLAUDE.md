# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Reference

```bash
# Install dependencies
make setup                          # Full setup (Python + Remotion + Piper TTS + HyperFrames cache)
pip install -r requirements.txt     # Python deps only

# GPU support
make install-gpu                    # Add local video generation models

# Testing
make test-contracts                 # Contract tests (no API keys needed)
python -m pytest tests/contracts/ -v -k <test_name>  # Single test
python -m pytest tests/ -v          # All tests

# Utilities
make preflight                      # Full provider menu dump
python -c "from tools.tool_registry import registry; registry.discover(); print(json.dumps(registry.provider_menu_summary(), indent=2))"  # Human-readable capability summary

# Demo
make demo                           # Render zero-key demo videos
```

## Read Before Working

**Mandatory first read:** [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — complete operating guide and agent contract. It contains routing rules that determine your first action based on what the user asked.

**Architecture reference:** [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — single source of truth for project architecture and conventions.

## Project Identity

OpenMontage is an **instruction-driven, agent-first video production system**. The AI agent IS the orchestrator. Python code only provides tools and persistence — no orchestration, review, or creative logic lives in Python.

## Core Architecture

```
Agent reads pipeline manifest (YAML) → reads stage director skill (MD)
→ uses tools (Python BaseTool) → self-reviews (meta skill)
→ checkpoints (Python utility) → presents to human for approval
```

### Three-Layer Knowledge System

| Layer | Location | Purpose |
|-------|----------|---------|
| Layer 1 | `tools/tool_registry.py` | What tools exist, their status, cost, runtime |
| Layer 2 | `skills/` | How OpenMontage wants tools used in pipelines |
| Layer 3 | `.agents/skills/` | Raw vendor/technology knowledge (prompt engineering, API rules) |

**Reading order:** Registry → Stage director skill → Layer 3 skill (before calling any generation tool).

## Pipeline System

### Pipeline State Machine
```
idea → script → scene_plan → assets → edit → compose
```

Each stage:
1. Reads its director skill (`skills/pipelines/<pipeline>/<stage>-director.md`)
2. Uses tools via the registry (not direct imports)
3. Self-reviews using `skills/meta/reviewer.md`
4. Checkpoints state via `lib/checkpoint.py`
5. Requests human approval at creative gates

### Pipeline Manifests
Declarative YAML files in `pipeline_defs/` defining stages, skills, tools, review criteria, and approval gates. 12 pipelines available (see `AGENT_GUIDE.md` for the full table).

### Adding a New Pipeline
1. Create YAML in `pipeline_defs/` (validated by `schemas/pipelines/pipeline_manifest.schema.json`)
2. Create stage director skills in `skills/pipelines/<pipeline>/` (idea through publish = 7 skills)
3. Reference meta skills in the manifest
4. Add compatible playbooks
5. Add contract tests in `tests/contracts/`

## Tool System

### Tool Contract
All tools inherit from `tools/base_tool.py` (`BaseTool`). Key fields:
- `name`, `version`, `tier` (A/B/C)
- `capability` (tts, video_generation, image_generation, music_generation, etc.)
- `provider` (elevenlabs, fal, openai, local, etc.)
- `runtime` (LOCAL, API, LOCAL_GPU, HYBRID)
- `status` (configured/unavailable)
- `agent_skills[]` — Layer 3 skills to read before calling
- `install_instructions` — env vars needed
- `dependencies` — grouped env var requirements

### Selector Pattern
Three selector tools route to capability-specific providers:
- `tts_selector` → all tools with `capability="tts"`
- `image_selector` → all tools with `capability="image_generation"`
- `video_selector` → all tools with `capability="video_generation"`

Selectors auto-discover providers from the registry. Adding a new provider tool automatically makes it available.

### Calling Tools
```python
from tools.tool_registry import registry
registry.discover()  # Always call first
tool = registry.get_tool("elevenlabs_tts")
result = tool.execute({"text": "Hello", "voice_id": "..."})
# result.success, result.data, result.error
```

**Tool class naming:** PascalCase without "Tool" suffix (e.g., `VideoCompose`, not `VideoComposeTool`).

## Composition Runtimes

`video_compose` has three render engines, chosen at proposal and locked in `edit_decisions.render_runtime`:

| Engine | Best For | Requires |
|--------|----------|----------|
| **FFmpeg** | Video cuts, concat, trim, subtitle burn | `ffmpeg` binary |
| **Remotion** | React-based composition: text cards, stat cards, charts, transitions, TalkingHead, word-level captions | Node.js + `remotion-composer/` |
| **HyperFrames** | HTML/CSS/GSAP: kinetic typography, product promos, website-to-video | Node.js ≥ 22 + `npx @hyperframes/cli` |

**Hard rule:** When both Remotion and HyperFrames are available, present BOTH to the user with recommendations before locking `render_runtime`. Log `render_runtime_selection` in `decision_log` with both options recorded.

### Remotion Scene Types
`remotion-composer/src/components/` — text_card, stat_card, callout, comparison, hero_title, terminal_scene, anime_scene, bar_chart, line_chart, pie_chart, kpi_grid, progress_bar. Overlay types: section_title, stat_reveal, hero_title, provider_chip.

## Artifact & Checkpoint System

### Canonical Artifacts
Each stage produces one validated artifact (schemas in `schemas/artifacts/`):
- `brief` → idea stage
- `script` → script stage
- `scene_plan` → scene_plan stage
- `asset_manifest` → assets stage
- `edit_decisions` → edit stage
- `render_report` → compose stage

### Checkpoints
- Location: `pipelines/<project_id>/checkpoint_<stage>.json`
- Schema: `schemas/checkpoints/checkpoint.schema.json`
- Status values: `completed`, `failed`, `awaiting_human`, `in_progress`
- Completed/awaiting_human checkpoints must include the canonical artifact

### Project Workspace
```
projects/<project-name>/
├── artifacts/          # JSON artifacts from each stage
├── assets/
│   ├── images/         # Generated images (PNG)
│   ├── video/          # Generated video clips (MP4)
│   ├── audio/          # Narration + final mix (MP3/WAV)
│   ├── music/          # Background music (MP3)
│   └── subtitles.srt   # Generated subtitles
└── renders/
    └── final.mp4       # Final deliverable
```
Directory is gitignored — all assets are regenerable. Use kebab-case names.

## Style Playbooks
YAML files in `styles/` defining visual language (typography, color, motion, audio). Validated by `schemas/styles/playbook.schema.json`.

| Playbook | Use Case |
|----------|----------|
| `clean-professional` | Corporate, educational, SaaS |
| `flat-motion-graphics` | Social media, TikTok, startups |
| `minimalist-diagram` | Technical deep-dives |

Loader: `styles/playbook_loader.py`

## Cost Governance
`tools/cost_tracker.py` implements: estimate → reserve → reconcile. Configurable modes: `observe` (track), `warn` (log overruns), `cap` (hard limit). Default cap: $10. Per-action approval threshold: $0.50.

## Key Infrastructure Files
- `lib/checkpoint.py` — read/write checkpoints, stage validation
- `lib/pipeline_loader.py` — manifest loading and helpers
- `lib/config_model.py` — Pydantic runtime config loader
- `lib/media_profiles.py` — platform-specific render profiles
- `tools/base_tool.py` — ToolContract base class
- `tools/tool_registry.py` — tool discovery and reporting
- `tools/cost_tracker.py` — budget governance
- `tools/video/video_compose.py` — runtime-aware composition orchestrator
- `tools/video/hyperframes_compose.py` — HyperFrames runtime
- `lib/hyperframes_style_bridge.py` — Playbook → CSS bridge for HyperFrames

## Runtime Requirements
- Python 3.10+
- FFmpeg
- Node.js 18+ (Remotion), Node.js ≥ 22 (HyperFrames)
- Optional: NVIDIA GPU for local video generation
- API keys are optional — many capabilities work with free/local tools

## Testing
```bash
# Contract tests (validate tool schemas, registry behavior, checkpoint logic)
make test-contracts

# Single contract test
python -m pytest tests/contracts/test_registry.py -v -k test_discover

# All tests
make test
```

QA integration tests live in `tests/qa/` for tool-by-tool output inspection.

## Communication Protocol
- Present capability menu via `provider_menu_summary()` before any creative work
- Announce provider/model choices before execution
- Ask before major changes (provider swap, model family change, render runtime swap)
- Log all decisions in `decision_log`
- Surface blockers with structured format (what, why, options, recommendation)
