# Reference Asset Gap Analyst — Meta Skill

Brief: Given a `video_analysis_brief.json` produced by `video_analyzer`, produce
a structured `asset_gaps` block (or sibling file) that the Backlot board
renders as "what the user needs to provide to recreate this." The goal is
actionable, question-bearing gaps — not a generic "you need images."

## When to Use

Trigger this skill when:

- A `video_analysis_brief.json` has just been written under
  `projects/<user>/<project>/analysis_<ts>/`.
- The brief's `_asset_gaps.status === "deterministic"` — meaning
  `state.py`'s pre-filler ran but the LLM hasn't enriched it yet.
- The user has approved a reference video and the next step is asking
  what they need to provide to recreate something like it.

Do NOT trigger when:

- No reference video was used (text-only brief — use
  `skills/meta/creative-intake.md` instead).
- The brief's `_asset_gaps.status === "llm_filled"` AND
  `filled_at` is within the last 10 minutes (idempotency — don't re-run
  unless the user explicitly asks for a refresh).
- The pipeline is a `talking-head` or `screen-demo` pipeline. Those are
  footage-led; the gap reduces to "provide footage" or "record your
  screen." Don't pad it with AI-generation recommendations.
- A `scene_plan.json` already exists. The pipeline has moved past the
  reference-intake phase — the panel is informational only at that point.

## Inputs

You will receive:

- **Brief path** — `projects/<user>/<project>/analysis_<ts>/video_analysis_brief.json`
- **Deterministic baseline** — the brief's `_asset_gaps` block, if
  present. This is your minimum-viable starting point.

Read these brief fields to inform the gap analysis:

| Field | What it tells you |
|---|---|
| `source.platform_metadata` | Uploader, view_count — context for "is this a known creator / licensed content" |
| `content_analysis.topics` | What content needs to be made |
| `content_analysis.key_claims` | Specific factual hooks to recreate |
| `content_analysis.tone` | Cinematic vs casual affects asset choices |
| `content_analysis.hook_technique` | The first 3 seconds drive the most expensive gap |
| `structure_analysis.scenes[].visual_type` | Per-scene visual type — drives asset categories |
| `structure_analysis.scenes[].motion_type` | Per-scene motion — drives generation strategy |
| `structure_analysis.pacing_profile.cuts_per_minute` | High-cut → cinematic SFX gap |
| `style_profile.music_style` | Audio gap driver |
| `style_profile.narration_style` | Voice gap driver |
| `style_profile.color_palette` | Visual identity gap |
| `style_profile.typography_observed` | Branded typography gap |
| `replication_guidance.key_elements_to_replicate` | The LLM-filled 5-aspect interpretation |
| `replication_guidance.elements_requiring_custom_work` | Pre-flagged custom-work items |
| `narration_transcript.segments[]` | Read the actual transcript for script gap analysis |
| `narration_transcript.word_count` | Word-count target for the user's chosen duration |

## Protocol

### Step 1: Read the brief

Open `video_analysis_brief.json`. Verify it parses. If the file is missing,
tell the user to run `video_analyzer` first and stop.

**Idempotency check.** If `_asset_gaps.status === "llm_filled"` and
`filled_at` is within the last 10 minutes, return without changes. Don't
re-enrich a fresh block unless the user asks.

If `filled_at` is older than 10 minutes, treat the existing `gaps[]` as a
prior version — refresh in place, preserve any user-answered gaps by
leaving them at `priority: "user_provided"` if that field exists.

### Step 2: Reuse the deterministic baseline

`_asset_gaps.gaps[]` from the deterministic pre-filler is your **minimum
viable** baseline. Keep every entry — but **augment, don't replace**.
The deterministic version captures mechanical facts ("70% motion_clip
scenes", "3 talking-head scenes"); the LLM pass adds:

- **Nuanced descriptions** — instead of "3 motion_clip scenes", say
  "the hero shot (0–3s) and the 2 product reveals need real-world
  footage or premium AI gen."
- **Specific questions** — instead of "旁白脚本", ask "你有 150 字的 hook
  吗？还是用 AI 起草？"
- **Priority reassessment** — the deterministic pre-filler puts
  everything at `must_have`. You might downgrade a music bed to
  `nice_to_have` if the brief's tone is documentary, or upgrade a
  brand-asset gap to `must_have` if the source has clear brand IP.
- **Scenario-specific options** — the deterministic offers 3 generic
  options; you add "Skip — use the original audio if rights permit" for
  music, or "stock + slight color shift" for footage you can't match.

### Step 3: Per-category gap analysis

For each category that has at least one brief signal, write one gap row.
Categories and their enrichment logic:

| Category | Brief signal | Enrichment |
|---|---|---|
| `video_footage` | `scenes[].visual_type in ["b_roll", "action", "aerial", "product_shot"]` AND `motion_type == "motion_clip"` | Identify hero shots (first 3s + peak moments) vs B-roll. Hero shots deserve real footage or premium AI gen; B-roll can be stock. Ask: "你希望 hero 镜头用实拍、AI 生成 (Kling / Seedance / Sora) 还是 stock？" |
| `images` | `motion_type == "animated_still"` count > 3 OR `visual_type in ["text_card", "diagram", "chart"]` | Distinguish product photos (need actual product), stock imagery (free-tier), or generated illustrations. The brief's `style_profile.color_palette` and `tone` drive the visual direction. |
| `script` | `narration_style.has_narration == true` AND `word_count > 50` | Read the actual `narration_transcript.segments[].text`. Compute target word count for the user's chosen duration (~150 wpm English, ~130 wpm Mandarin). Ask: "保留原脚本的语序（合规风险），还是基于相同结构重写？" |
| `narration` | Same as `script` but specifically about voice casting | Recommend a TTS voice profile based on the brief's observed narration style (gender, age, pacing). Suggest provider based on availability — don't hardcode; defer to `tts_selector`. |
| `music` | `style_profile.music_style` truthy OR `tone in ("cinematic", "dramatic", "inspirational")` | Distinguish library vs. generated. If the user has a `music_library/` folder in their project, surface that. Otherwise recommend MusicGen / ACE-Step / ElevenLabs Music. Ask: "原视频音乐如果未授权 — 同风格生成，还是换成 royalty-free 库？" |
| `sfx` | `tone in ("cinematic", "dramatic", "action")` AND `cuts_per_minute >= 8` | High-cut cinematic videos need sound design. Brief examples based on observed content: whoosh on transitions, impact on text reveals, room tone, etc. |
| `brand` | `visual_type in ["product_shot", "pack_shot"]` count > 0 OR `topics[]` mentions specific brands | Ask: "如果有真实产品 / logo，请提供高分辨率图；否则我们用文字或 placeholder." **Critical:** if the source video contains third-party brand IP, the gap MUST include a rights question. |
| `character` | `visual_type in ["talking_head", "portrait"]` count > 0 OR `subject_motion` includes facial actions | Distinguish real human (needs release form), avatar (HeyGen / Synthesia / Kling avatar), AI-generated character (FLUX portrait → avatar-pipeline). If the source has identifiable humans, surface a likeness-rights gap. |
| `text` | `scenes[].on_screen_text` count > 0 OR `typography_observed` truthy | If the source has branded typography, surface the font / brand identity as a gap. If just generic titles, can be templated from the chosen playbook. |

If a category has zero signals, **do not** add a row for it. The
deterministic pre-filler sometimes inflates gaps (e.g., adding a `text`
gap to a video with no on-screen text); you may prune those.

### Step 4: Format and write back

Write the enriched gap object as a **sibling file**:

```
projects/<user>/<project>/analysis_<ts>/asset_gaps.json
```

Do NOT mutate `video_analysis_brief.json` — it is the source of truth
from the tool and must stay pristine. Backlot's `state.py` should look
for the sibling first and fall back to the brief's `_asset_gaps` block
if the sibling is missing.

**Schema:**

```json
{
  "status": "llm_filled",
  "filled_at": "<ISO 8601 timestamp>",
  "summary": "<1-2 sentences, e.g. '3 must-have gaps, 2 nice-to-have. The reference uses cinematic B-roll plus on-screen text cards — the most expensive gap is the hero footage (3 motion_clip scenes in the first 10s).'>",
  "gaps": [
    {
      "id": "<kebab-case, e.g. 'hero_footage'>",
      "category": "<one of: video_footage, images, script, narration, music, sfx, brand, character, text>",
      "label": "<3-7 words, e.g. 'Hero 镜头 (0-3s)'>",
      "description": "<1-2 sentences, specific to this brief>",
      "can_ai_generate": true | false,
      "priority": "must_have" | "nice_to_have" | "optional",
      "question_for_user": "<one-line question, or null if obvious>",
      "options": ["<2-5 short labels the user can pick from>"]
    }
  ]
}
```

**Field semantics:**

- `id` — kebab-case slug. Used as React key in Backlot. Never reuse the
  same id within one brief — Backlot's `diff` would misalign.
- `category` — one of the 9 enum values above. Adding new categories
  requires a Backlot panel update.
- `label` — short, scannable. The Backlot panel uses this as the card
  title.
- `description` — must reference **specific scenes / timestamps** from
  the brief. Generic descriptions ("you need video") are a regression.
- `can_ai_generate` — false when rights/IP prevents AI generation, or
  when only real footage makes sense (e.g., a specific person's face).
- `priority` — `must_have` blocks the pipeline; `nice_to_have` is
  surfaced in proposal; `optional` is auto-resolved with a default.
- `question_for_user` — `null` when the answer is obvious from context.
- `options` — 2-5 short labels. The user picks one; their selection
  feeds the `assets` stage. Skip if no decision is required.

### Step 5: Notify the user

After writing the file, surface the gaps to the user in chat. Use a
tight format — the Backlot panel has the structured list; the chat is
the conversational summary:

```
Analysis complete — [N] must-have gaps, [M] optional.

Must-have:
1. Hero footage (0-3s) — opening impression; needs AI video gen or real shoot
2. Product images — [N] product shots
3. Voiceover script — [N] words, ~[X] seconds at 150 wpm

Optional:
- Background music (cinematic bed)
- Subtitle styling

Questions:
- Do you have existing product shots, or use text/placeholder?
- Hero footage budget: real shoot (expensive) / AI gen (mid) / stock (cheap)?
```

Keep the chat surface short. The full structured list lives in
`asset_gaps.json` and renders in Backlot.

## Output Contract

This skill must produce TWO things:

1. **`asset_gaps.json`** at `projects/<user>/<project>/analysis_<ts>/`
   — per Step 4 schema.
2. **A short user-facing message** — per Step 5 format.

It must NOT produce:

- A copy of the brief (the brief already exists).
- A generic "asset requirements" document — the gaps are brief-specific.
- Stage 2+ artifacts (scene_plan, script, proposal). Those belong to
  the next pipeline stage.

## Layer 3 Skill Gate (MANDATORY before generation)

This skill produces **gap recommendations**. The recommendations may
trigger downstream tools (image gen, video gen, TTS, music gen) once
the user answers. Before writing prompts to those tools, read their
Layer 3 skills:

- If recommending TTS → read `.agents/skills/elevenlabs/` or
  `text-to-speech`
- If recommending video gen → read `.agents/skills/ai-video-gen/` and
  provider-specific (`seedance-2-0`, `ltx2`, `kling-official`)
- If recommending music gen → read `.agents/skills/acestep/` or
  `music`
- If recommending image gen → read `.agents/skills/flux-best-practices/`

This is the same governance rule as `video-reference-analyst.md` §
"Step 4b: Layer 3 Skill Gate." The skill's gap output should cite
which downstream tools each gap routes to — that hint lives in the
`options[]` field.

## Error Handling

| Failure | Action |
|---|---|
| Brief file not found | Tell user to run `video_analyzer` first; stop. |
| Brief is malformed | Read what's readable, surface partial gaps with a warning in `summary`, don't crash. Mark the file with `status: "partial"` instead of `"llm_filled"`. |
| `video_analyzer` step failed (e.g., no transcript) | Use `style_profile` + `content_analysis.summary` as fallback; note the missing fields in `asset_gaps.summary` so the user knows the gaps are based on partial analysis. |
| Deterministic baseline contradicts brief signals | Trust the brief signals — the deterministic pre-filler is mechanical and sometimes wrong. Override with a `description` that explains the contradiction. |
| User already started the pipeline (`scene_plan.json` exists) | Don't re-run; just inform the user the panel is informational at this point. Don't write a new `asset_gaps.json` — it would race with the in-flight pipeline. |
| All gap categories are `optional` | The reference is generic enough that nothing must be provided. Skip the panel update entirely; tell the user "this reference has no specific asset gaps — proceed to proposal." |

## Examples

### Example 1: YouTube Shorts dance challenge

**Brief excerpt:**

```json
{
  "source": {"duration_seconds": 58, "type": "shorts"},
  "content_analysis": {"tone": "entertaining", "hook_technique": "drop into mid-move at frame 1"},
  "structure_analysis": {
    "total_scenes": 12,
    "scenes": [
      {"scene_index": 0, "start_time": 0, "end_time": 4, "visual_type": "b_roll", "motion_type": "motion_clip"},
      {"scene_index": 1, "start_time": 4, "end_time": 9, "visual_type": "b_roll", "motion_type": "motion_clip"}
    ],
    "pacing_profile": {"cuts_per_minute": 14, "pacing_style": "rapid_fire"}
  },
  "style_profile": {
    "music_style": "trap pop, 140 bpm",
    "narration_style": {"has_narration": false}
  }
}
```

**Derived `asset_gaps.json`:**

```json
{
  "status": "llm_filled",
  "filled_at": "2026-09-05T14:32:11Z",
  "summary": "2 must-have gaps. The most expensive is real dance footage — original audio is a known artist so we can't reuse it.",
  "gaps": [
    {
      "id": "hero_dance_footage",
      "category": "video_footage",
      "label": "Hero dance clip (0-9s)",
      "description": "The opening 9 seconds is 2 rapid motion_clip scenes of the creator dancing. AI video gen struggles with full-body choreography, so this likely needs real footage.",
      "can_ai_generate": true,
      "priority": "must_have",
      "question_for_user": "Will you film the dance yourself, or use AI generation (Kling Motion Brush)?",
      "options": ["Self-record", "AI gen (Kling)", "Stock dance clip", "Skip dance format"]
    },
    {
      "id": "music_licens",
      "category": "music",
      "label": "Music (140 bpm trap pop)",
      "description": "Original audio is a known artist — likely unlicensed. Need to source a same-tempo replacement.",
      "can_ai_generate": true,
      "priority": "must_have",
      "question_for_user": "Same-tempo replacement from where?",
      "options": ["Generate (MusicGen)", "Library (Epidemic Sound)", "Skip — go silent"]
    }
  ]
}
```

**Reasoning:** Hero footage is the load-bearing scene; music is the
rights minefield. SFX was a candidate (cuts_per_minute = 14) but
deferred — beat-synced cuts work without explicit SFX if the music is
strong.

### Example 2: Tech explainer (3Blue1Brown style)

**Brief excerpt:**

```json
{
  "source": {"duration_seconds": 720, "type": "youtube"},
  "content_analysis": {"tone": "educational", "topics": ["neural networks", "backpropagation"]},
  "structure_analysis": {
    "total_scenes": 80,
    "scenes": [
      {"scene_index": 0, "visual_type": "animation", "motion_type": "animated_still"}
    ],
    "pacing_profile": {"cuts_per_minute": 6, "pacing_style": "steady_educational"}
  },
  "style_profile": {
    "narration_style": {"has_narration": true, "words_per_minute": 130},
    "music_style": null
  },
  "narration_transcript": {"word_count": 1480}
}
```

**Derived `asset_gaps.json`:**

```json
{
  "status": "llm_filled",
  "filled_at": "2026-09-05T14:32:11Z",
  "summary": "3 must-have gaps. The work is animation (80 scenes of Manim-style math); voice is critical for comprehension.",
  "gaps": [
    {
      "id": "math_animations",
      "category": "video_footage",
      "label": "Math animations (80 scenes)",
      "description": "Reference is ~80 Manim-style animated stills. Production requires Manim or Remotion-Math equivalent. Most expensive single cost.",
      "can_ai_generate": false,
      "priority": "must_have",
      "question_for_user": "Animation mode — Manim (math-native) or Remotion (easier general motion)?",
      "options": ["Manim", "Remotion + LaTeX", "Hybrig Motion Graphics"]
    },
    {
      "id": "voiceover_script",
      "category": "script",
      "label": "Voiceover script (~1500 words)",
      "description": "Reference is 1480 words at 130 wpm; a 12-min explainer at your target duration should land in the same range.",
      "can_ai_generate": false,
      "priority": "must_have",
      "question_for_user": "Script source — write it yourself, or draft from your bullet points?",
      "options": ["I provide", "Draft from bullet points", "Reuse reference structure (rights check)"]
    },
    {
      "id": "narration_voice",
      "category": "narration",
      "label": "Narrator voice (neutral, slow)",
      "description": "Reference uses a calm, paced delivery (~130 wpm). Pair with a neutral-accent English voice.",
      "can_ai_generate": true,
      "priority": "must_have",
      "question_for_user": null,
      "options": ["ElevenLabs — 'Adam' (deep)", "ElevenLabs — 'Rachel' (neutral)", "Local Piper voice"]
    }
  ]
}
```

**Reasoning:** Animation is the work, voice is critical for
comprehension. Music is optional — the reference has none. Brand/character
gaps don't apply.

### Example 3: Product launch cinematic

**Brief excerpt:**

```json
{
  "source": {"duration_seconds": 30, "type": "youtube"},
  "content_analysis": {"tone": "cinematic", "topics": ["XR headset", "spatial computing"]},
  "structure_analysis": {
    "total_scenes": 8,
    "scenes": [
      {"scene_index": 0, "visual_type": "product_shot", "motion_type": "motion_clip"},
      {"scene_index": 3, "visual_type": "product_shot", "motion_type": "motion_clip"}
    ],
    "pacing_profile": {"cuts_per_minute": 16, "pacing_style": "dynamic_social"}
  },
  "style_profile": {
    "music_style": "orchestral swell, cinematic",
    "color_palette": {"primary_colors": ["#0A0A0A", "#FFFFFF"], "accent_colors": ["#C8A35C"]}
  }
}
```

**Derived `asset_gaps.json`:**

```json
{
  "status": "llm_filled",
  "filled_at": "2026-09-05T14:32:11Z",
  "summary": "3 must-have, 1 nice-to-have. Brand IP is the rights minefield; cinematic needs proper sound design.",
  "gaps": [
    {
      "id": "hero_product_shots",
      "category": "video_footage",
      "label": "Hero product shots (scenes 0, 3)",
      "description": "Reference opens on a beauty-shot of the headset and returns to it at scene 3. Hero shots — high-quality AI gen or real shoot.",
      "can_ai_generate": true,
      "priority": "must_have",
      "question_for_user": "Hero footage source — real product photography (best) or AI product gen (Kling / Seedance)?",
      "options": ["Real product photos", "AI product gen", "Stock headset imagery", "Skip cinematic format"]
    },
    {
      "id": "brand_assets",
      "category": "brand",
      "label": "Brand assets (logo, color guide)",
      "description": "Reference uses #0A0A0A + #C8A35C — looks like a specific brand's palette. Need to confirm: is this your brand, or a different company whose IP we're sampling?",
      "can_ai_generate": false,
      "priority": "must_have",
      "question_for_user": "Confirm — is the gold/black palette yours, or a third-party brand we're inspired by?",
      "options": ["Our brand — I'll send logo + palette", "Inspired by third-party — use a neutral palette", "Open to suggestions"]
    },
    {
      "id": "cinematic_music",
      "category": "music",
      "label": "Cinematic orchestral bed",
      "description": "30-second cinematic needs an orchestral swell. Reference audio is custom — generate a same-style replacement.",
      "can_ai_generate": true,
      "priority": "must_have",
      "question_for_user": null,
      "options": ["ElevenLabs Music (orchestral)", "ACE-Step (cinematic)", "Library (Artlist cinematic)"]
    },
    {
      "id": "sfx_design",
      "category": "sfx",
      "label": "SFX (risers, impacts, room tone)",
      "description": "16 cuts/min cinematic needs sound design — whoosh on transitions, impact on text reveals, room tone under voiceover.",
      "can_ai_generate": true,
      "priority": "nice_to_have",
      "question_for_user": null,
      "options": ["ElevenLabs SFX", "Library (Boom Library)", "Skip — let music carry"]
    }
  ]
}
```

**Reasoning:** Brand IP is the rights minefield — must ask. Cinematic
needs proper sound design. Music is `must_have` because the reference
audio is custom and not licensable.

## Integration with Backlot

The output of this skill flows into Backlot through this sequence:

1. Skill writes `asset_gaps.json` sibling to `video_analysis_brief.json`.
2. Backlot's watcher detects the new file (mtime + content hash).
3. Backlot's `state.py` (`_load_reference_brief`) loads:
   - `video_analysis_brief.json` for classification (topic, tone,
     scenes).
   - Sibling `asset_gaps.json` if present → use it.
   - Otherwise, fall back to `brief._asset_gaps` (deterministic).
   - Otherwise, run the deterministic pre-filler inline.
4. Backlot's reference panel shows:
   - Classification block (from brief).
   - Gap cards (from whichever source was loaded).

This skill only writes the file — the panel wiring lives in
`backlot/state.py` and `backlot/web/`. If the panel does not update
after writing, the watcher / state load is the bug, not this skill.

## See Also

- `skills/meta/video-reference-analyst.md` — produces the brief this
  skill reads.
- `skills/meta/creative-intake.md` — the text-only intake path.
- `schemas/artifacts/video_analysis_brief.schema.json` — exact field
  names referenced above.
- `backlot/README.md` — how the live storyboard derives panel state.