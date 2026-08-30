# PROGRESS — HyperFrames Blueprint Transformer

**Status:** Halted per user instruction on 2026-08-28. Resumable.

## Plan

Authoritative plan: `/root/.claude/plans/agile-zooming-hopcroft.md`
(claude plan workflow file). Follow that — this progress file only
tracks implementation state.

## What is DONE (10 of 13 tasks)

| Task | Output | Status |
|------|--------|--------|
| 1. Env verify | node 22.22.1, python 3.10.12, pydantic 2.13.4, pytest 9.1.1, ffmpeg/ffprobe at `/usr/bin/`. **Did NOT actually run** `npx hyperframes --version` (user declined that npx call). | ✅ |
| 2. Skeleton | `transformer/`, `fixtures/`, `tests/`, `scripts/`, `pyproject.toml`, `.gitignore` | ✅ |
| 3. models.py | Strict Pydantic schema, 8 SceneTypes, Format/Sence/TargetBlueprint | ✅ |
| 4. mapping.py | `scene_to_cut` + `detect_cut_kind` + `compute_timeline` + `build_audio_refs` | ✅ |
| 5. scene_workers.py | ProcessPoolExecutor with `workers` knob, `executor.map` preserves order, per-worker `pid` log line | ✅ |
| 6. render_adapter.py | `_stage_assets`, `_build_asset_manifest`, `_bridge_audio`, `render_via_existing_tool` calling `HyperFramesCompose.execute()` | ✅ |
| 7. orchestrator.py | `run()`, `_load_blueprint`, `_resolve_asset_lookup`, `RunResult` dataclass | ✅ |
| 8. cli.py + __main__.py | argparse, exit codes (0/2/3), `--no-render` flag | ✅ |
| 9. Fixtures | `target_blueprint.example.json` (6 scenes per MVP §40 acceptance), `bag-front.png`, `bag-side.png`, `feature-back.png`, `lifestyle.mp4` (4s via ffmpeg lavfi) | ✅ |
| 10a. test_models.py | 8 tests: load fixture, strict format, enum rejection, negative duration rejection, transition whitelist, sorted_scenes, extras rejection, transition default | ✅ |
| 10b. test_mapping.py | 12 tests: each of 7 scene types, fallback behavior, asset lookup miss, timeline math, `detect_cut_kind` matrix | ✅ |
| 10c. test_scene_workers.py | 5 tests: order preservation, empty scenes, workers=1, length mismatch raises, log line per scene | ✅ |

Total code written: ~620 lines Python + ~80 lines shell/json fixtures.

## What is NOT DONE (3 of 13 tasks)

### Task #10d: `test_orchestrator_smoke.py` (~30 lines, ~10 min)
End-to-end test with `--no-render`:
- Use the `tmp_path` pytest fixture for workspace.
- Run `orchestrator.run(blueprint_path, workspace_root=tmp_path, render=False, workers=2)`.
- Assert:
  - `cuts.json` exists at `<tmp_path>/projects/demo_proj/cuts.json`.
  - `cuts.json` parses as JSON with `len(cuts) == 6`.
  - `result.cut_count == 6`, `result.total_duration_seconds > 0`.
  - All `cuts[i].in_seconds` strictly monotonic.

### Task #11: scripts (`scripts/run_demo.sh` + `scripts/verify_render.sh`) (~30 lines, ~15 min)
- `run_demo.sh` — bash wrapper that invokes `python -m transformer run --blueprint fixtures/... --workspace data --quality draft --verbose`, with set -euo pipefail.
- `verify_render.sh` — `ffprobe` the output MP4 and assert 1080x1920 / 30fps / h264 / duration ≈ sum(scene.duration).

### Task #12: README.md (~80 lines, ~15 min)
- One-paragraph "why this exists".
- 5-step usage example (install → fill blueprint → run → verify).
- **The headline comparison table:** Remotion (MVP doc §4.5) vs HyperFrames (this prototype) across data-driven, batch, existing investment, learning curve, scene-by-scene effort, visual control ceiling.

### Task #13: Run pytest + run_demo.sh end-to-end (needs HyperFrames runtime installed)
- Tests (1-10d) all run offline; no `npx hyperframes` invoked by tests.
- Real e2e requires resolving HyperFrames runtime. Open questions:
  - The user earlier declined `npx --yes hyperframes` (it triggers npm fetch).
  - If the runtime is not pre-installed, `HyperFramesCompose.execute(render)` returns success=False with `runtime_check.reasons` listed. `run_demo.sh` should fall back to "skip render, log the runtime_check message" rather than fail loudly.
  - Per plan §"Scope" we don't silently swap renderers — `verify_render.sh` should be conditional on the MP4 actually existing.

## Quick resume checklist for next session

```bash
cd /opt/OpenMontage_Voicebox/hyperframes-blueprint-transformer

# 1. Confirm tests 10a-c still pass (sanity check, should be green):
python3 -m pytest tests/test_models.py tests/test_mapping.py tests/test_scene_workers.py -v

# 2. Finish 10d: write tests/test_orchestrator_smoke.py

# 3. Run all tests:
python3 -m pytest tests/ -v

# 4. Write scripts/run_demo.sh and scripts/verify_render.sh

# 5. Write README.md (esp. the Remotion vs HyperFrames table from plan §"Verification Step 3")

# 6. Try a real e2e:
bash scripts/run_demo.sh          # may need npx hyperframes runtime installed
bash scripts/verify_render.sh      # only meaningful after step 6 produces final.mp4
```

## User-stated decisions (do NOT re-litigate on resume)

1. **End-to-end rendering is in scope** — `--render` flag is the default, `--no-render` is the escape hatch.
2. **Multiprocess granularity is per-scene** — `ProcessPoolExecutor` over scenes, default `workers = min(cpu_count(), 8)`.
3. **Independent R&D project location** — top-level `hyperframes-blueprint-transformer/`, NOT under `tools/` (this is a research client of `HyperFramesCompose`, not a base capability in the OpenMontage registry).
4. **Do NOT modify `tools/video/hyperframes_compose.py`** — research project consumes its public `execute()` only.

## Files persisted (all on disk)

```
hyperframes-blueprint-transformer/
├── .gitignore
├── pyproject.toml
├── PROGRESS.md                              ← this file
├── transformer/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── models.py
│   ├── mapping.py
│   ├── scene_workers.py
│   ├── render_adapter.py
│   └── orchestrator.py
├── fixtures/
│   ├── target_blueprint.example.json
│   └── assets/{bag-front,bag-side,feature-back}.png + lifestyle.mp4
└── tests/
    ├── __init__.py
    ├── test_models.py
    ├── test_mapping.py
    └── test_scene_workers.py
```

Plus plan file at `/root/.claude/plans/agile-zooming-hopcroft.md`.

Nothing is in-memory that isn't on disk. Safe to halt.
