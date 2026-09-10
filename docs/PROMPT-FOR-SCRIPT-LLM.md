# Prompt Template — Script Generation for OpenMontage Rendering

> **Purpose**: This is a copy-paste prompt you give to another LLM (the "script LLM"). The script LLM will write a complete video script; **you** then hand the JSON output to Claude (the "renderer") which will run OM's `cinematic` pipeline to render the final mp4.
>
> **Snapshot**: 2026-09-10. Reflects live OM capabilities (`docs/PIPELINES-AND-OFFLINE-CAPABILITIES.md`) and live assets (`docs/RESOURCES.md`).
>
> **Companion docs** (the script LLM should skim these for context, not re-read in full):
> - [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — what OM can and can't do
> - [`RESOURCES.md`](RESOURCES.md) — what assets already exist on disk
> - [`MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) — audio gotchas, loop recipes, license traps
> - [`INDEX.md`](INDEX.md) — docs/ folder map

---

## Part 1 — The Prompt to Send to the Script LLM

Copy the block below into the script LLM (Claude, GPT-4, Gemini, etc.) — verbatim. Replace the bracketed fields `[LIKE_THIS]` with your project specifics.

---

```
You are writing a video script for OpenMontage, an AI video production
platform. The script you produce will be handed to a renderer that will
execute it via OM's `cinematic` pipeline.

Your output must be a single JSON document — no prose explanation, no
markdown formatting around it, no code fences. Just the JSON. The
renderer will not parse anything except valid JSON.

## Product / brief

- Product: [PRODUCT_NAME, e.g. "小鹏哥 潮牌旅行箱"]
- Brand: [BRAND_NAME, e.g. "小鹏哥 (Xiaopeng Ge)"]
- Target audience: [e.g. "Gen-Z urban professionals, 22-32, TikTok-first"]
- Tone / style: [e.g. "TikTok vertical, fast-paced, EU/US aesthetic but
  with Chinese subtitles. Bright, optimistic, not dark or moody."]
- Target video duration: [TOTAL_SECONDS, e.g. 30 or 60]
- Aspect ratio: [9:16 / 16:9 / 1:1, default 9:16 for TikTok]
- Language: [e.g. "Chinese narration (Simplified) + English on-screen text OK"]
- Must-include features: [LIST 3-5 product features to highlight]

## Hard constraints (the renderer will reject the script if violated)

- 5–7 scenes total. Each scene: 4–8 seconds. Sum must equal target
  duration ±1 s.
- Every scene MUST declare `type` ∈ {"T2V","I2V","first+last frame"}.
- T2V scenes (no image): `prompt` only, no `first_frame_image`.
- I2V scenes: `prompt` AND `first_frame_image` MUST be a public HTTPS URL
  OR a data:image/...;base64,... URI. Local paths will be rejected.
- `model` MUST be "MiniMax-H3" or "MiniMax-H3-Max" (H3-Max = faster +
  cheaper; H3 = better quality + supports multi-image reference).
- `duration` MUST be 5–15 s per scene (H3-Max hard floor is 5 s; H3 is 4 s).
- `resolution` MUST match model: H3-Max = "480P" or "768P"; H3 = "768P"
  or "2K". Other values will be rejected.
- `ratio` MUST be set per scene. For T2V: a concrete ratio (NOT "adaptive")
  like "9:16". For I2V: "adaptive" or a concrete ratio.
- Subtitles: short (≤12 chars per scene). Match the visual beat.
- Voiceover text: full narration string for the entire video, ≤150
  Chinese characters total.
- Total output JSON size: < 5 MB. (Large data URIs count.)

## Visual style guide (apply to every scene's `prompt`)

- Cinematic but TikTok-fast — camera movement in every shot (dolly-in,
  handheld follow, push-in, parallax). No static frames.
- Lighting: bright, optimistic, daylight or warm interior. NO neon
  night / moody-dark / silhouette / "black with neon spillover".
  The renderer will adjust color grade to "bright_clean" anyway, but
  prompts should not fight it.
- Composition: rule of thirds, shallow depth of field, the product
  featured prominently in at least 60% of frames.
- People: if any, 25-32 year-old, casual style, diverse — but
  characters are optional and most scenes work fine product-only.
- Style keywords to include in prompts: "cinematic", "high energy",
  "commercial", "modern", "sharp detail". Avoid: "dark", "moody",
  "cinematic dark", "low key".

## Background music (do NOT specify in the script — pre-assigned)

- The renderer will use `/tmp/pixabay_happy.mp3` (124.6 s, CC0,
  "Optimistic Pleasant Light Music" by alex-morgan, RMS 7362) as the
  primary BGM. Bright, optimistic, no lyrics. The script does NOT
  need to specify music — it's auto-selected from RESOURCES.md.
- If you need a specific mood that's clearly incompatible with happy
  optimistic (e.g. a horror reveal), add a `bgm_override: true` field
  in the top-level `audio` block and the renderer will re-evaluate.

## Voiceover

- Voice: female Mandarin, `zf_xiaobei` (Kokoro-82M, 24 kHz).
- One continuous narration string for the whole video, ≤150 chars.
- Per-scene timestamps are derived from `start_seconds`/`end_seconds`,
  do NOT include timing markers in the text.

## Subtitles

- Burned into the video via ffmpeg drawtext (Noto Sans CJK SC Bold).
- One subtitle line per scene, ≤12 Chinese characters, in scene's
  `subtitle_text` field.
- The renderer positions subtitles in lower-third (114, 1180, 540×120)
  by default; trust that.

## Existing assets you can reference

- First-frame product stills at: `https://ocbot.aixifs.com/videopic/<name>.png`
  — confirm any URL you reference is actually uploaded there. If you
  reference a URL that doesn't exist, the renderer will surface the 2013
  error and the run will fail.
- Background music: `/tmp/pixabay_happy.mp3` (described above).
- Voice: Kokoro-82M / `zf_xiaobei`.

## Output schema (strict)

Output EXACTLY this shape:

{
  "project_meta": {
    "project_id": "kebab-case-id",
    "title": "<short title>",
    "target_duration_seconds": <int>,
    "ratio": "9:16",
    "category": "product-acquisition",
    "color_grade": "bright_clean",
    "language": "zh",
    "audience": "<one line>",
    "tone": "<one line>"
  },
  "audio": {
    "bgm_track": "/tmp/pixabay_happy.mp3",
    "voice": {
      "enabled": true,
      "voice_id": "zf_xiaobei",
      "provider": "kokoro_tts",
      "text": "<full narration ≤150 chars>"
    }
  },
  "scenes": [
    {
      "scene_id": "scene-1-hook",
      "type": "T2V",
      "start_seconds": 0,
      "end_seconds": 5,
      "prompt": "<English cinematic prompt, 1-2 sentences, no text/logo in frame>",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "≤12 Chinese chars",
      "overlay": null
    },
    {
      "scene_id": "scene-2-product",
      "type": "I2V",
      "start_seconds": 5,
      "end_seconds": 10,
      "prompt": "<...>",
      "first_frame_image": "https://...png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "...",
      "overlay": null
    }
    // ... 3-5 more scenes, sum durations = target_duration_seconds
  ],
  "cta": {
    "scene_id": "scene-final-cta",
    "type": "T2V",
    "start_seconds": <total - 3>,
    "end_seconds": <total>,
    "prompt": "<CTA-focused prompt, brand-relevant>",
    "first_frame_image": null,
    "model": "MiniMax-H3-Max",
    "resolution": "768P",
    "ratio": "9:16",
    "subtitle_text": "立即购买 / 戳链接",
    "overlay": "img-cta-qr-code"
  }
}

Validation rules the renderer will check:
- scene.end_seconds - scene.start_seconds ∈ [4, 8] (per scene)
- sum(scenes[].end_seconds - scenes[].start_seconds) == target_duration_seconds
- every first_frame_image is either null, https://..., or data:image/...;base64,...
- every model ∈ {"MiniMax-H3", "MiniMax-H3-Max"}
- every resolution matches model constraint (H3-Max: 480P|768P; H3: 768P|2K)
- voice.text ≤ 150 Chinese chars
- subtitle_text ≤ 12 Chinese chars per scene
- output JSON < 5 MB

Begin. Output the JSON only.
```

---

## Part 2 — Walkthrough of Why Each Constraint Exists

For the human user (you), not the script LLM. Useful when reviewing the script LLM's output to catch issues before handing to the renderer.

### Why `5–8` seconds per scene?

- H3 / H3-Max video generation has a **hard floor of 5 s** (H3-Max) / 4 s (H3). A 3-s scene would 4xx.
- 5 s is the minimum billable unit for kapon's paygo pricing — 4-s requests get rounded up.
- 8 s is the maximum where you can fit 5–7 scenes in 30 s without rushing the visual story.
- If the target is 60 s, scenes can stretch to 10–12 s (but be careful: long shots are harder to QA for visual drift).

### Why MiniMax-H3 / H3-Max only?

- All other online video providers on this host are **either unavailable (no API key) or exhausted** (Hailuo Token Plan 4-shot 2067 cap).
- These two models are routed through the **kapon OneHub proxy** with a Bearer token (`KAPON_API_TOKEN` in `.env`).
- See [`MUSIC-ISSUES-AND-FIXES.md`](../docs/MUSIC-ISSUES-AND-FIXES.md) for the I2V URL requirement (must be public HTTPS — kapon datacenter IPs get blocked by imgbb etc.).

### Why `bright_clean` color grade?

- Today's `cinematic` pipeline defaults to `moody_dark` (avg luminance 18–52 / 255) — unsuitable for office demo.
- `bright_clean` lifts shadow toe and midtones (avg luminance target 80–110 / 255). The ffmpeg `curves` filter does this after rendering.
- **Prompt-level reinforcement**: do NOT use "dark"/"moody"/"low key" in prompts. The prompt and the color grade fight each other.

### Why `Kokoro / zf_xiaobei` voiceover?

- Kokoro-82M is the only **offline-capable high-quality Mandarin TTS** on this host.
- `zf_xiaobei` is the default Mandarin female voice (warm, clear, professional).
- See [`MUSIC-ISSUES-AND-FIXES.md`](../docs/MUSIC-ISSUES-AND-FIXES.md) §1 for the 40-second warm-up caveat — the renderer batches all voice into one Kokoro call to amortize.

### Why `pixabay_happy.mp3` as default BGM?

- CC0-equivalent (commercial use OK, no attribution required).
- Bright, optimistic, low-key, RMS energy 7362 — fits an OM capability demo without being saccharine.
- Already downloaded; renderer doesn't need to re-fetch.
- See [`RESOURCES.md`](../docs/RESOURCES.md) §3 for the comparison table and license verification.

### Why `<5 MB` JSON output cap?

- The MCP Streamable-HTTP transport has practical body-size limits around 5–10 MB on this host. Larger payloads risk timeout or 413.
- Data URIs count toward this. If you need a 4-MB first-frame, you have ~1 MB of headroom for the rest of the script. If you need bigger, **host the image on `ocbot.aixifs.com/videopic/` first** and reference by URL.

---

## Part 3 — Failure Modes the Script LLM Should Pre-Avoid

The script LLM, reading only the prompt block, may not anticipate these. If the renderer fails, it's most likely one of these:

| Failure | Cause | Fix |
|---|---|---|
| `kapon HTTP 403 model_not_allowed` | The KAPON_API_TOKEN doesn't have H3 / H3-Max permission. | Re-check the kapon console. If only H3 is allowed, switch all scenes to H3. |
| `kapon HTTP 400 illegal base64 at input byte 4` | The script passed a data URI but Base64 was malformed (Bash `base64 -w0` is fine; Python `base64.urlsafe_b64encode` is NOT). | Use Python `base64.b64encode` (standard alphabet). |
| `kapon HTTP 2013 media url unreachable` | The `first_frame_image` URL is on imgbb / another anti-bot-blocked CDN. kapon's datacenter IP gets 403 from imgbb. | Host the image on `https://ocbot.aixifs.com/videopic/` and reference that URL. |
| Scene rendering stalls / times out at 600 s | The kapon generation queue is congested (peaks during EU business hours). | Renderer retries once; if still failing, escalate or queue for off-peak. |
| Generated video is shorter than requested | Some providers under-run by 0.1–0.5 s on the trailing frame. | Renderer pads with a hold-last-frame freeze to match the planned duration. |
| Generated video has a black final frame | Some models return Success with an empty `content.url`. | Renderer retries; if 2nd attempt also empty, the script LLM is asked to regenerate the scene. |

The script LLM should design around these: use H3-Max over H3 (more reliable), use HTTPS URLs over data URIs when possible, keep prompts under 7000 chars (H3 hard limit), avoid non-ASCII in `prompt` (Kokoro handles CJK fine, but kapon's tokenizer sometimes drops diacritics).

---

## Part 4 — End-to-End Render Pipeline (After Script LLM Produces JSON)

What happens when you hand the JSON to the renderer:

1. **Validate JSON** against the schema in Part 1. Reject and surface errors.
2. **Pre-flight**:
   - Generate first-frame PNGs (if any scene is T2V without first_frame_image, generate via `minimax_image`).
   - Upload all first-frame PNGs to `https://ocbot.aixifs.com/videopic/`.
   - Render BGM at target duration via ffmpeg loop + fade (recipe in [`RESOURCES.md`](../docs/RESOURCES.md) §5).
   - Synthesize Kokoro voiceover from `audio.voice.text` (single call).
3. **Per-scene generation**:
   - Submit to kapon v2 endpoint with `model`, `duration`, `resolution`, `ratio`, `content[]`.
   - Poll until Success/Fail (typical 60–120 s for 5–8 s clip).
   - Download signed URL → save as `renders/scene_N.mp4`.
4. **Per-scene post**:
   - Scale to target resolution if needed.
   - Burn `subtitle_text` via ffmpeg `drawtext` (Noto Sans CJK SC).
   - Apply overlay (CTA / logo) via ffmpeg `overlay`.
5. **Compose**:
   - Concat all scenes via ffmpeg concat demuxer.
   - Mux voiceover + BGM via `amix` (BGM duck under voice, `sidechaincompress`).
   - Apply color grade `bright_clean` via ffmpeg `curves + eq`.
   - Output `final_30s_v1.mp4`.
6. **QA**: spot-check 5 frames, audio loudness, duration. Run `final_review.json`-equivalent checks.
7. **Deliver**: report `final_<duration>_v1.mp4` path; if QA fails, surface specific issue and request re-render of the offending scene.

The whole pipeline typically runs in **5–10 minutes** for a 30 s video, mostly dominated by kapon's per-scene generation latency.

---

## Part 5 — Quick-Start Workflow

1. Copy Part 1 verbatim, replace the `[LIKE_THIS]` brackets.
2. Send to your preferred script LLM.
3. Paste the JSON output back to the renderer.
4. Renderer validates, generates, composes — produces the final mp4.
5. Spot-check the result; iterate on `prompt` per scene if visuals drift.

If the script LLM has questions about OM capabilities, point it at [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md). If it has questions about available assets, point it at [`RESOURCES.md`](RESOURCES.md).

---

## Appendix — Real Worked Example (Xiaopeng Ge Travel Luggage TikTok)

For reference, here is a **skeleton** the script LLM might produce. This is **not for copy-paste** — it's to show the expected shape.

```json
{
  "project_meta": {
    "project_id": "xiaopeng-ge-tiktok-luggage-30s",
    "title": "小鹏哥 · 城市旅行箱 30s",
    "target_duration_seconds": 30,
    "ratio": "9:16",
    "category": "product-acquisition",
    "color_grade": "bright_clean",
    "language": "zh",
    "audience": "Gen-Z urban professionals, 22-32, TikTok-first",
    "tone": "Fast-paced, bright, optimistic, EU/US aesthetic with Chinese subtitles"
  },
  "audio": {
    "bgm_track": "/tmp/pixabay_happy.mp3",
    "voice": {
      "enabled": true,
      "voice_id": "zf_xiaobei",
      "provider": "kokoro_tts",
      "text": "小鹏哥潮牌旅行箱，德国拜耳PC材质，TSA海关锁，一拉就走。今天的旅程，明天就到了。"
    }
  },
  "scenes": [
    {
      "scene_id": "scene-1-hook",
      "type": "T2V",
      "start_seconds": 0,
      "end_seconds": 5,
      "prompt": "Cinematic tracking shot following a young traveler wheeling a sleek hardshell suitcase through a sunlit European cobblestone street, slow dolly-in, shallow depth of field, sharp modern commercial, no text no logo no person face",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "一拉就走",
      "overlay": null
    },
    {
      "scene_id": "scene-2-detail",
      "type": "I2V",
      "start_seconds": 5,
      "end_seconds": 11,
      "prompt": "Close-up of hardshell suitcase surface with reflective texture, daylight, slow push-in, shallow DOF, sharp detail",
      "first_frame_image": "https://ocbot.aixifs.com/videopic/xiaopeng-ge-detail.png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "拜耳PC材质",
      "overlay": null
    },
    {
      "scene_id": "scene-3-feature",
      "type": "I2V",
      "start_seconds": 11,
      "end_seconds": 17,
      "prompt": "Hand operating the TSA lock on a hardshell suitcase, daylight interior, fast cuts feel, sharp detail",
      "first_frame_image": "https://ocbot.aixifs.com/videopic/xiaopeng-ge-tsa-lock.png",
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "TSA海关锁",
      "overlay": null
    },
    {
      "scene_id": "scene-4-lifestyle",
      "type": "T2V",
      "start_seconds": 17,
      "end_seconds": 23,
      "prompt": "Young traveler with suitcase in a sunny airport terminal, cinematic dolly-in, bright optimistic commercial mood, no text no logo",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "今天的旅程",
      "overlay": null
    },
    {
      "scene_id": "scene-5-cta",
      "type": "T2V",
      "start_seconds": 23,
      "end_seconds": 30,
      "prompt": "Hardshell suitcase centered on bright minimal background with subtle motion blur of wheels rolling forward, commercial product shot, bright daylight, no text no logo",
      "first_frame_image": null,
      "model": "MiniMax-H3-Max",
      "resolution": "768P",
      "ratio": "9:16",
      "subtitle_text": "戳链接立即购买",
      "overlay": "img-cta-qr-code"
    }
  ],
  "cta": {
    "scene_id": "scene-final-cta",
    "type": "T2V",
    "start_seconds": 27,
    "end_seconds": 30,
    "prompt": "Same as scene-5",
    "first_frame_image": null,
    "model": "MiniMax-H3-Max",
    "resolution": "768P",
    "ratio": "9:16",
    "subtitle_text": "戳链接",
    "overlay": "img-cta-qr-code"
  }
}
```

Note: in this example the `cta` block is redundant with the last scene — the renderer will dedup. The script LLM should include either the last scene or the `cta` block, not both. (This is a known quirk in the schema; v2 will remove `cta` as a top-level field.)
