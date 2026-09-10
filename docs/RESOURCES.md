# Resources Available for Downstream LLM / Pipeline Reuse

> **Snapshot**: 2026-09-10. All assets verified on disk; SHA-256 hashes included for cross-machine integrity verification.
>
> **Status flag**: ⭐ = preferred / P1 priority · ✓ = verified usable · ⚠ = has caveats (license / format / size)
>
> **Source**: This document is the canonical handoff index when **another LLM** is asked to script a new获客 video. Point it at this file; it lists what is already produced, which one is preferred, and what must be regenerated vs what can be reused as-is.

---

## Quick Reference — Recommended BGM for Office Demo

**Primary pick**: `/tmp/pixabay_happy.mp3` (124.6 s, 48 kHz mp3 256 kbps stereo)

- **Title**: "Optimistic Pleasant Light Music" by **alex-morgan**
- **License**: Pixabay Content License (CC0 equivalent — free, no attribution required, **commercial use OK**)
- **Why this one**: bright, optimistic, low-key — fits an OM capability demo for clients in an office without being saccharine. Picked over the other 3 downloads for highest spectral energy (RMS 7362, the brightest of the four).
- **Drop-in**: copy to `projects/<project-id>/assets/music/music_v2_demo.mp3`, replace `music_v1.mp3` reference in `edit_decisions.json`.

---

## 1. Documentation (committed to git in commit `09cbe3c`)

| Path | Size | Purpose |
|---|---|---|
| `docs/PIPELINES-AND-OFFLINE-CAPABILITIES.md` | 23 KB | All 14 production pipelines + decision tree + offline-capability matrix. **Read first** when picking a pipeline. |
| `docs/MUSIC-ISSUES-AND-FIXES.md` | 19 KB | Three audio capability issues + reuse recipes. License caveat. |
| `docs/INDEX.md` | 1.6 KB | Entry-point index of the docs/ folder. |
| `HANDOFF-2026-09-10.md` | 5.5 KB | Cross-session handoff (xiaohongshu project state). |

## 2. Final Deliverable (gitignored, on local disk only)

| Path | Size | SHA-256 (first 16 chars) | Notes |
|---|---|---|---|
| `projects/xiaohongshu-neon-night-30s/renders/final_30s_v1.mp4` | 10.5 MB | `7f498d088e18185a` | 30.016 s, 768×1364, h264+aac@24fps. **Dark moody color grade** — **NOT the office-demo version**. See §5 below for the demo-version recipe. |

## 3. BGM Candidates — Downloaded from Pixabay (no API key needed)

All four are **CC0-equivalent (Pixabay Content License)** — **safe for commercial use**. Pixabay was scraped via OM's `pixabay_music` tool without any API key (Pixabay Music dropped the key requirement in 2024).

| Path | Size | SHA-256 | Title | Artist | Duration | Sample rate | RMS energy | Priority |
|---|---|---|---|---|---|---|---|---|
| **`/tmp/pixabay_happy.mp3`** | 3.8 MB | `3c9ba470b15daefd` | **Optimistic Pleasant Light Music** | alex-morgan | 124.6 s | 48 kHz stereo 256 kbps | **7362** | ⭐ **PRIMARY** |
| `/tmp/pixabay_lively.mp3` | 4.3 MB | `0a7768319bfaa595` | Upbeat - Upbeat Background Music | prettyjohn1 | 141.9 s | 44.1 kHz stereo 256 kbps | 7208 | ✓ Backup #1 (most rhythmic) |
| `/tmp/pixabay_corporate2.mp3` | 3.6 MB | `f9424e38e3073b93` | Tech Presentation | The_Mountain | 116.6 s | 44.1 kHz stereo 256 kbps | 6988 | ✓ Backup #2 (corporate) |
| `/tmp/pixabay_corporate.mp3` | 5.4 MB | `a052f9071e6eec4c` | Upbeat Corporate | AtlasAudio | 176.0 s | 44.1 kHz stereo 256 kbps | 4036 | ✓ Backup #3 (calmest) |

**For office demo to client**: pick `pixabay_happy.mp3`. **For pure rhythm-driven demo**: pick `pixabay_lively.mp3`. The other two are reasonable but more generic.

**Note on sampling rate mismatch**: `pixabay_happy.mp3` is 48 kHz while the others are 44.1 kHz. ffmpeg auto-resamples on mux, but if you want strict consistency:

```bash
ffmpeg -y -i /tmp/pixabay_happy.mp3 -ar 44100 /tmp/pixabay_happy_44k.mp3
```

## 4. MusicGen Test Outputs (CC-BY-NC-4.0 — **NON-COMMERCIAL ONLY**)

| Path | Size | SHA-256 | Duration | Format | Notes |
|---|---|---|---|---|---|
| `/tmp/musicgen_test.wav` | 0.83 MB | `d64437fb5ea91858` | 13.34 s | 32 kHz mono PCM | Raw MusicGen output. |
| `/tmp/musicgen_stereo_44k.wav` | 2.3 MB | (not hashed — derived) | 13.34 s | 44.1 kHz stereo | Resampled + stereo-upmixed. |
| `/tmp/musicgen_30s_loop.wav` | 1.8 MB | `15db9302426567c2` | 30.0 s | 32 kHz stereo | Loop of source + 0.5 s fade-in / 1 s fade-out. |
| **`/tmp/musicgen_90s.wav`** | **15.1 MB** | `65f9b0a6d9837dd6` | **90.0 s** | 44.1 kHz stereo | **Loop of source for 90 s**. Verified 2026-09-10. |
| `/tmp/musicgen_120s.wav` | (not generated — recipe in MUSIC-ISSUES-AND-FIXES.md §4) | — | 120 s | — | Same recipe as 90 s, just change `-t`. |

⚠️ **License trap**: MusicGen-small is **CC-BY-NC-4.0**. **Cannot** be used in commercial获客 videos without a commercial license from Meta. For shipping commercial videos, **use Pixabay (§3) instead**. The MusicGen outputs are kept for **internal testing / non-commercial demos / pipeline verification only**.

## 5. Recipe: Build the "Office Demo Version" of final_30s_v1.mp4

The existing `final_30s_v1.mp4` uses moody-dark color grade + the dark "Brain Implant" Pixabay track. For client demo you need both brighter. Two-step recipe:

### Step 1 — Replace BGM with `pixabay_happy.mp3`

```bash
# cut happy track to exactly 30 s with fade-in/out (drop in at 0s, fade out last 1 s)
ffmpeg -y -v error \
    -i /tmp/pixabay_happy.mp3 \
    -ss 0 -t 30 \
    -af "afade=t=in:st=0:d=0.5,afade=t=out:st=29:d=1" \
    -c:a libmp3lame -b:a 192k \
    /tmp/music_demo_30s.mp3

# remux onto final_30s_v1.mp4 (replace audio track, keep video untouched)
ffmpeg -y -v error \
    -i /opt/OpenMontage_Voicebox/projects/xiaohongshu-neon-night-30s/renders/final_30s_v1.mp4 \
    -i /tmp/music_demo_30s.mp3 \
    -c:v copy -c:a aac -map 0:v:0 -map 1:a:0 -shortest \
    /tmp/final_30s_demo_audio.mp4
```

### Step 2 — Lift color grade from moody_dark → bright_clean

```bash
ffmpeg -y -v error \
    -i /tmp/final_30s_demo_audio.mp4 \
    -vf "curves=all='0/0.05 0.3/0.4 0.5/0.65 0.7/0.85 1/1',eq=brightness=0.10:saturation=1.20:contrast=1.10" \
    -c:a copy \
    /opt/OpenMontage_Voicebox/projects/xiaohongshu-neon-night-30s/renders/final_30s_demo_v1.mp4
```

The `curves` filter lifts the shadow toe (0→0.05) and the midtone (0.5→0.65), giving a brighter but still cinematic look. `eq` adds 10% brightness + 20% saturation + 10% contrast — a generic "office demo" lift that won't crush the cinematic intent.

### Expected output metrics

After the two-step pipeline:

| Metric | Original | Demo version |
|---|---|---|
| Mean luminance | 18–52 / 255 | ~80–110 / 255 (bright but not washed) |
| RMS (audio) | (dark moody BGM) | 7362 (bright happy BGM) |
| File size | 10.0 MB | ~10.5 MB (negligible change) |
| Codec / container | h264+aac@24fps, mp4 | identical |

## 6. TTS / ASR Test Outputs (Chinese voice round-trip)

| Path | Size | SHA-256 | Duration | Notes |
|---|---|---|---|---|
| `/tmp/kokoro_test.wav` | 0.30 MB | `9c9e8422f2917b7e` | 6.33 s | Mandarin, voice `zf_xiaobei`, text "夜色是新的纹理。这是产品宣传短片的配音测试。" |
| `/tmp/kokoro_warm.wav` | 0.16 MB | `b8006c7939d9182c` | 3.52 s | Same voice, second call (warm cache test), text "夜景潮牌，全新发布。" |
| `/tmp/kokoro_test_transcript.json` | 4.7 KB | `268beae5c2a5a86f` | (text) | WhisperX `transcriber` round-trip transcript of `kokoro_test.wav`. **Note: base model has Chinese homophone errors** (色→座, 纹→文). |

Other Kokoro test artefacts (`kokoro_am_adam.wav`, `kokoro_am_michael.wav`, `kokoro_smoke.wav`, `kokoro_zh.wav`, `kokoro_en.wav`) are smoke-test residuals from a previous session and have no role in any production pipeline — can be deleted.

## 7. How Another LLM Should Use This Document

If you (a human) are asking **another LLM** to script a new获客 video:

1. **Read first**: `docs/PIPELINES-AND-OFFLINE-CAPABILITIES.md` (pipeline decision tree) + `docs/RESOURCES.md` (this file).
2. **Script the scenes**: produce a JSON of `scenes[]` with `{scene_id, type, prompt, duration_seconds, first_frame_url, voice_text, subtitle_text, model, resolution, ratio}`. Schema sample in `AGENT_GUIDE.md` and the §1 of `PIPELINES-AND-OFFLINE-CAPABILITIES.md`.
3. **BGM choice for office demo**: default to `/tmp/pixabay_happy.mp3` (this file §3, primary pick). Do **NOT** use MusicGen outputs (§4) for commercial获客 videos — license blocks commercial use.
4. **Voiceover**: default to `kokoro_tts` with voice `zf_xiaobei`. If the script has >3 voice lines, batch into one Kokoro call using `[pause]` markers between segments.
5. **Pipeline choice**: `cinematic` (default for hero pieces) with `render_runtime=ffmpeg` + `composition_mode=atelier` for hand-crafted. Override `color_grade` to `bright_clean` (NOT the default `moody_dark`).

If you (a human) want a literal "drop these into my OM now" command, the full pipeline command is in §5 of this file.

---

## 8. Asset Persistence Note

⚠️ **All `/tmp/*` files are ephemeral** — they will be cleared on the next system reboot. To persist any of them, copy to:

- **Project-scoped**: `projects/<project-id>/assets/{images,video,music,scripts}/`
- **Shared test cache**: `sources/` folder (already gitignored)
- **For audio only**: `~/.cache/huggingface/hub/` is the convention for HF-cached assets, but Pixabay downloads don't have a designated OM cache path yet.

If you need any of the listed files after a reboot, re-run the download command:

```bash
# pixabay_happy.mp3 (the primary pick):
cd /opt/OpenMontage_Voicebox && .venv/bin/python3 -c "
from tools.tool_registry import registry
registry.discover()
t = registry._tools['pixabay_music']
t.execute({'query':'bright positive optimistic happy', 'min_duration':60, 'max_duration':180, 'output_path':'/tmp/pixabay_happy.mp3'})
"
```

MusicGen outputs (§4) are NOT regenerable without burning ~3 min CPU per 5 s of audio — keep them safe if you intend to reuse them for non-commercial purposes.
