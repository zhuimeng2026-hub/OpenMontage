# Compose Stage Plan — Remix Luggage v1

- **Date written**: 2026-09-05 13:40 UTC
- **Project**: `projects/remix-luggage-v1/`
- **Pipeline**: video-template-remix
- **Stage**: compose (gated, `human_approval_default: true`)

This document is a handoff for the cron-triggered continuation 3 hours after
writing (target fire time: 2026-09-05 16:40 UTC). It is intentionally
self-contained — the agent picking this up should not need to re-derive
context from earlier conversations.

---

## TL;DR

Run the compose stage end-to-end and produce `render_report.json` +
`final_review.json` for the user to approve. Concretely:

1. Read `compose-director.md` skill (mandatory before any tool call).
2. Pre-flight: confirm `video_compose` is still available with
   `render_engines.remotion=True`; confirm `audio_mixer` is available.
3. Resolve pre-production blockers (see §5) — most importantly, TTS
   sample approval + broll footage readiness.
4. Run `video_compose` with `render_runtime=remotion` from the
   approved `edit_decisions.json`.
5. Run `audio_mixer` to mix narration + BGM + SFX per the audio
   config in `edit_decisions.json`.
6. Run `video_stitch` (if needed) to concat composed video with mixed
   audio.
7. Run a verification pass (ffprobe duration check, visual QA on a
   sampled frame per scene).
8. Write `render_report.json` + `final_review.json` (both required by
   the compose stage).
9. Write `checkpoint_compose.json` with `status="awaiting_human"`.
10. Stop and present results to the user.

---

## 1. Pipeline State — Where We Are Now

All artifacts up through `edit_decisions` are produced, schema-validated,
and (for the gated stages) human-approved. None of the artifacts have
been re-read since the last commit-style edit; verify each schema
before composing.

```
projects/remix-luggage-v1/
├── project.json
├── source/
│   └── research_brief.json   ← VLM-filled, source-grounded
├── artifacts/
│   ├── brief.json            ← status: locked, angle=B + remotion
│   ├── decision_log.json     ← 5 decisions, d-001=remotion d-002=angle_b locked
│   ├── script.json           ← 9 sections, 118s total
│   ├── scene_plan.json       ← 41 scenes, 20.85 cuts/min, timeline gap-free
│   ├── asset_manifest.json   ← 30 assets (9 narration + 9 video + 4 sfx + ...)
│   └── edit_decisions.json   ← 41 cuts + 9 overlays + audio + subtitles
├── checkpoint_idea.json          ← completed
├── checkpoint_script.json        ← completed
├── checkpoint_scene_plan.json    ← completed
├── checkpoint_assets.json       ← completed
└── checkpoint_edit.json          ← completed (human_approval_default: false)
```

No `checkpoint_compose.json` yet. No `render_report.json`. No
`final_review.json`. No actual rendered video file. **That is the
gap the compose stage closes.**

---

## 2. Inputs the Compose Stage Will Consume

| Input | Source | Required |
|---|---|---|
| `edit_decisions.json` | artifacts/edit_decisions.json | YES — primary driver |
| `asset_manifest.json` | artifacts/asset_manifest.json | YES — every cut's source |
| `scene_plan.json` | artifacts/scene_plan.json | YES — frame-rate / aspect |
| `render_runtime` | locked `remotion` (d-001) | YES — non-negotiable |
| `composition_mode` | `templated` | YES — uses Remotion stock scene-types |
| `renderer_family` | `presenter` | YES — selects editor + broll pattern |
| `compose_target` | 720x1280 30fps cover fit | YES |
| `fps` | 30 | YES |
| `font` | `SourceHanSans-Bold` (SIL OFL) | YES — for burned captions |
| `lut` | `prosumer_natural.cube` | OPTIONAL — color grade |

### Cuts × Asset Map

41 cuts must each be resolved to a real asset path. The mapping built
by edit-decisions generation is:

| scene type | asset source | source_tool |
|---|---|---|
| talking_head (16 cuts) | user_recorded face_*.mp4 (asset_talking_head_*) | user_recorded |
| broll (6 cuts) | user_recorded broll_*.mp4 (asset_broll_*) | user_recorded |
| animation (5 cuts) | Remotion scene-type configs (asset_animation_*) | remotion_compose |
| text_card (3 cuts) | caption_burn configs | subtitle_gen → remotion |
| transition (1 cut) | asset_animation_transition | remotion_compose |

**Critical gap**: as of writing, the user has NOT confirmed broll
footage is shot. The compose stage will block on missing broll assets
unless the user has uploaded them by 16:40 UTC. See §5.

---

## 3. Tool Calls the Compose Stage Will Make

### 3.1 Pre-flight (mandatory per AGENT_GUIDE.md §"Mandatory Preflight")

```python
from tools.tool_registry import registry
registry.discover()
print(registry._tools["video_compose"].get_info()["render_engines"])
# expected: {"ffmpeg": True, "remotion": True, "hyperframes": True}
```

If `remotion` is no longer available, this is a BLOCKER. Do NOT silently
substitute ffmpeg/hyperframes — surface the blocker per
"Escalate Blockers Explicitly" rule. The user already approved
remotion at the proposal stage; switching runtimes requires re-approval.

### 3.2 Render — primary call

```python
result = registry._tools["video_compose"].execute({
    "edit_decisions_path": "projects/remix-luggage-v1/artifacts/edit_decisions.json",
    "asset_manifest_path": "projects/remix-luggage-v1/artifacts/asset_manifest.json",
    "render_runtime": "remotion",
    "composition_mode": "templated",
    "output_path": "projects/remix-luggage-v1/renders/final.mp4",
    "project_id": "remix-luggage-v1",
    "userid": "local-dev",
    # Optional: explicit entry composition
    # "composition_id": "LuggageRemixV1",
})
```

If the tool's input schema differs (some versions nest under
`inputs` or `arguments`), check the live schema via
`registry._tools["video_compose"].input_schema` before calling.

### 3.3 Audio mix

```python
mix = registry._tools["audio_mixer"].execute({
    "narration_segments": [...],   # from edit_decisions.audio.narration.segments
    "music": edit_decisions.audio.music,
    "sfx": edit_decisions.audio.sfx,
    "output_path": "projects/remix-luggage-v1/renders/audio_mix.wav",
})
```

### 3.4 Stitch (if video_compose doesn't mux internally)

```python
stitch = registry._tools["video_stitch"].execute({
    "video_path": "projects/remix-luggage-v1/renders/final.mp4",
    "audio_path": "projects/remix-luggage-v1/renders/audio_mix.wav",
    "output_path": "projects/remix-luggage-v1/renders/final_with_audio.mp4",
})
```

### 3.5 Verification

```python
import subprocess, json
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries",
     "stream=codec_type,codec_name,width,height,r_frame_rate",
     "-show_entries", "format=duration,size",
     "-of", "json", "projects/remix-luggage-v1/renders/final_with_audio.mp4"],
    capture_output=True, text=True,
)
info = json.loads(probe.stdout)
assert info["format"]["duration"] == "118.000", "duration drift"
```

---

## 4. Outputs the Compose Stage Must Produce

### 4.1 `projects/remix-luggage-v1/artifacts/render_report.json`

Schema lives at `schemas/artifacts/render_report.schema.json`. Required
fields include (verify against schema before writing):

- `output_path` — absolute path to rendered mp4
- `duration_seconds` — must equal 118.000 (script total)
- `resolution` — 720x1280
- `fps` — 30
- `file_size_bytes`
- `render_runtime` — `remotion`
- `composition_mode` — `templated`
- `render_duration_seconds` — wall-clock render time
- `cuts_resolved` — list of (cut_id → asset_id) confirmations
- `cuts_unresolved` — list of (cut_id → reason) — must be empty for
  the compose stage to advance
- `warnings` — non-blocking issues

### 4.2 `projects/remix-luggage-v1/artifacts/final_review.json`

Schema lives at `schemas/artifacts/final_review.schema.json`. Required
fields:

- `passes` — boolean (true only if duration/resolution/audio all
  verify)
- `checks` — list of named QA checks (duration, aspect_ratio,
  audio_levels, subtitle_burn_visible, sample_frame_qa)
- `sample_frames` — paths to 4-6 sampled frames at 0/25/50/75/100%
  progress (for visual QA)
- `recommendations` — list of optional polish items

### 4.3 `projects/remix-luggage-v1/checkpoint_compose.json`

```python
ckpt = write_checkpoint(
    pipeline_dir=Path("projects"),
    project_id="remix-luggage-v1",
    stage="compose",
    status="awaiting_human",
    artifacts={
        "brief": brief, "decision_log": dec, "script": script,
        "scene_plan": sp, "asset_manifest": am,
        "edit_decisions": ed,
        "render_report": render_report, "final_review": final_review,
    },
    human_approval_required=True,
    human_approved=False,
    review={...,
    metadata={
        "stage": "compose",
        "review_focus": ["Visual quality acceptable",
                         "Audio levels correct",
                         "Duration drift = 0"],
        "render_runtime": "remotion",
        "render_duration_seconds": ...,
        "next_stage": "publish",  # if pipeline has publish stage
    },
)
```

---

## 5. Pre-Production Blockers (must be resolved before compose runs)

These are the conditions that, if unmet, BLOCK the compose stage. The
cron continuation agent should check each one in order.

### 5.1 Broll footage readiness — HIGHEST RISK

`asset_broll_luggage_l01` (stairs fall), `asset_broll_luggage_l02_handle`
(handle break), `asset_broll_luggage_l03_test` (composite test),
`asset_broll_luggage_l04_montage` (3x fall montage),
`asset_broll_luggage_l05_wheel` (wheel fly),
`asset_broll_luggages_panorama` (5-piece panorama) — all 6 broll files
must exist at the asset_manifest paths:

```
projects/remix-luggage-v1/assets/video/
├── broll_l01_299_stairs_fall.mp4
├── broll_l02_599_handle_break.mp4
├── broll_l03_composite_test.mp4
├── broll_l04_1299_montage.mp4
├── broll_l05_1999_wheel_fly.mp4
└── broll_5_luggages_panorama.mp4
```

If any are missing, the user must upload them before 16:40 UTC, OR the
agent must offer to substitute with stock footage (pexels/pixabay) per
their explicit approval. **Do NOT proceed with placeholder stills.**

### 5.2 TTS sample approval

`asset_tts_narration_test_3_luggage` is the sample section per
`script.json.voice_performance.sample_section_id`. The user must approve
the TTS sample BEFORE the other 8 narration segments are batched.
Currently `voice_performance.sample_approved = false` on all 9.

If the user has not approved by 16:40 UTC, the agent should pause and
ask — don't auto-approve. The 9-segment batch generation is expensive
and re-runnable, but generating them and then discovering the sample
was wrong wastes time.

### 5.3 BGM selection

`asset_bgm_main` should be picked via `music_search_pixabay` per
decision_log d-003. If the user has uploaded their own track to
`music_library/`, prefer that.

### 5.4 Font + LUT availability

`asset_font_chinese_bold` (思源黑体粗体) — verify
`assets/fonts/SourceHanSans-Bold.otf` exists. If not, fallback to
`/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc` (most Linux
systems have Noto CJK).

`asset_lut_prosumer` (`assets/luts/prosumer_natural.cube`) — Remotion
needs a `.cube` LUT file. If absent, skip the LUT step (Remotion has a
default neutral LUT).

---

## 6. Risks / Known Gotchas

### 6.1 Remotion render can be slow

`remotion-composer/` + Node.js + chrome-headless-shell renders can
take 5-15 minutes for a 118-second 9:16 video. Set timeout ≥ 20 min.
Monitor memory — `video_compose` reports `cpu_cores=2, ram_mb=2048`.

### 6.2 Audio mixing requires ffmpeg on PATH

`audio_mixer` shells out to ffmpeg. Verify with `which ffmpeg` before
calling — see §3.3.

### 6.3 The reminder check after `video_compose`

After compose, the audio may be missing if the video was rendered with
video-only. ALWAYS verify with ffprobe that the output mp4 has an
audio stream. If not, run audio_mixer + stitch (or a single ffmpeg
mux).

### 6.4 The narration asset doesn't exist yet

The TTS samples have not been generated. If by 16:40 UTC they are not
on disk at `assets/audio/tts_*.wav`, the agent must:

1. Run `transcriber` (no — that's wrong, transcriber is for input
   transcription). Instead:
2. Run the `tts` registry tool (find via
   `registry.get_by_capability("tts")` — Kokoro is available per
   memory, DashScope CosyVoice is the fallback).
3. Or escalate to the user.

### 6.5 The `video_analyzer` silent-failure bug is still unfixed

`docs/bugs/video-analyzer-keyframe-silent-failure-2026-09-05.md` —
NOT relevant to compose stage (we already produced all artifacts).
Just noting it for context.

---

## 7. Decision Log Updates

After compose completes, append these decisions to
`projects/remix-luggage-v1/artifacts/decision_log.json` (append-only,
do not modify existing entries):

```json
{
  "decision_id": "d-006",
  "stage": "compose",
  "category": "music_source",
  "subject": "实际选用的 BGM",
  "options_considered": [
    {"option_id": "pixabay_xxx", "label": "Pixabay 上检索的曲子", "score": 0.8, "reason": "..."},
    {"option_id": "user_uploaded_xxx", "label": "用户上传的曲子", "score": 0.5, "reason": "..."}
  ],
  "selected": "...",
  "reason": "..."
},
{
  "decision_id": "d-007",
  "stage": "compose",
  "category": "provider_selection",
  "subject": "实际调用的 TTS provider",
  "options_considered": [
    {"option_id": "kokoro", "label": "Kokoro zh-0 base", "score": 0.7, "reason": "..."},
    {"option_id": "dashscope_cosyvoice", "label": "DashScope CosyVoice", "score": 0.3, "reason": "..."}
  ],
  "selected": "...",
  "reason": "..."
}
```

---

## 8. Post-Compose Handoff

After `checkpoint_compose.json` is written as `awaiting_human`:

1. Present a summary to the user:
   - Render duration (wall-clock)
   - Output file path + size
   - QA check results (from final_review.json)
   - 4-6 sampled frame thumbnails for visual approval
2. Wait for user to approve / request revision / abort.

If approved → the pipeline may continue to a `publish` stage if
defined; otherwise the run is complete.

---

## 9. Files to Re-Read Before Compose (for verification)

- `skills/pipelines/video-template-remix/compose-director.md` — the
  actual compose skill, which may have evolved since writing this plan
- `tools/video/video_compose.py` — verify input_schema is unchanged
- `tools/audio/audio_mixer.py` — verify input_schema is unchanged
- `schemas/artifacts/render_report.schema.json` — verify required
  fields are unchanged
- `schemas/artifacts/final_review.schema.json` — verify required
  fields are unchanged

If any of these have changed materially, adapt the compose call
accordingly. The schema-validate step (after writing each artifact)
will catch most drift.

---

## 10. Self-Test Before the Real Compose

Before the actual render, run a 1-second probe render to confirm the
Remotion pipeline boots correctly:

```python
test = registry._tools["video_compose"].execute({
    "edit_decisions_path": "...",
    "asset_manifest_path": "...",
    "render_runtime": "remotion",
    "composition_mode": "templated",
    "output_path": "projects/remix-luggage-v1/renders/_probe.mp4",
    "duration_seconds": 1,  # test param — verify support
})
```

If the tool doesn't support a duration override, just do the full
118s render and accept the 5-15 min cost. If the render errors, read
the error and decide: surface blocker (per "Escalate Blockers
Explicitly") or fall back to a different runtime after user approval.

---

**End of plan. Agent picking this up at 16:40 UTC: start at §3.1.**