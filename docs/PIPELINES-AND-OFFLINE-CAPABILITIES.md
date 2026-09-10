# OM Pipelines & Offline Capability Matrix

> **Snapshot date**: 2026-09-10. Verified against live registry (`python -c "from tools.tool_registry import registry; registry.discover(); print(registry.provider_menu_summary())"`) and the on-disk HF cache at `~/.cache/huggingface/hub/`.
>
> **Scope**: every entry in `pipeline_defs/*.yaml` plus the BaseTool capabilities reachable WITHOUT calling an external paid service (Hailuo / ElevenLabs / OpenAI / fal.ai / HeyGen / kling / etc). The single known **online-bridged** capability, `minimax_h3_kapon` (H3 / H3-Max via kapon OneHub proxy), is documented at the bottom for completeness but excluded from the "offline" tally.
>
> **Companion docs**:
> - [`AGENT_GUIDE.md`](../AGENT_GUIDE.md) — operating contract for the agent
> - [`PROJECT_CONTEXT.md`](../PROJECT_CONTEXT.md) — architecture single source of truth
> - [`docs/PROVIDERS.md`](PROVIDERS.md) — every paid provider with pricing
> - [`MCP_SERVER.md`](../MCP_SERVER.md) — MCP tool surface contract

---

## Part 1 — All 14 Production Pipelines

Each pipeline lives at `pipeline_defs/<name>.yaml`. Stages chain `research → proposal → script → scene_plan → assets → edit → compose → publish` (some pipelines skip or replace stages). All pipelines use **executive-producer** orchestration mode unless noted.

| # | Pipeline | Category | Budget USD | Stability | One-liner |
|---|---|---|---|---|---|
| 1 | `video-template-remix` | custom | 0.25 | production | **Reference-faithful remix.** Take a source video URL → extract shot map / timing / transitions / subtitles / audio structure → reproduce same shape with new content. Cheapest. |
| 2 | `framework-smoke` | custom | — | test | Minimal manifest that exercises Phase-0 framework contracts only. Not for real production. |
| 3 | `talking-head` | talking_head | 0.50 | production | End-to-end talking-head video. Takes raw footage of a person speaking, transcribes, makes edit decisions, generates b-roll, composes. Needs source footage. |
| 4 | `clip-factory` | custom | 1.00 | production | Multi-clip extraction. Long-form (webinar / stream / presentation / interview) → multiple short clips. |
| 5 | `podcast-repurpose` | custom | 1.00 | production | Podcast audio or video-podcast → audiogram or visual podcast snippets. |
| 6 | `screen-demo` | screen_recording | 1.00 | production | Screen recording pipeline. Two modes: pure screencast or screencast + presenter. |
| 7 | `documentary-montage` | documentary | 1.00 | production | **Retrieval-first thematic montage.** Semantic corpus from Pexels / Archive.org / Pixabay. **Gated `edit` stage** (unusual). |
| 8 | `animated-explainer` | generated | 2.00 | production | Generated explainer from topic/idea — fully AI-produced with narration, visuals, music. |
| 9 | `animation` | animation | 2.00 | production | Motion graphics, kinetic typography, math visuals, stylized illustrations. |
| 10 | `character-animation` | animation | 2.00 | production | Local reusable cartoon characters. Script + scene plan → character speech / acting. **No voice / TTS stage.** |
| 11 | `avatar-spokesperson` | custom | 2.00 | production | Presenter-led avatar: spokesperson, internal updates, onboarding, sales intros. **All avatar providers are currently `unavailable` on this host** — see Part 3. |
| 12 | `cinematic` | cinematic | 2.00 | production | **Mood-led cinematic for trailers, brand films, short-form dramatic edits.** Reference video support. EP orchestration with quality gates on emotional pacing / color consistency / audio dynamics. **Recommended for product-marketing hero content.** |
| 13 | `hybrid` | hybrid | 2.00 | production | Source footage combined with designed or generated support assets: interviews + diagrams / b-roll + animated titles. |
| 14 | `localization-dub` | custom | 3.00 | production | Transcript-first localization: translated subtitles + dubbed audio + optional lip-sync. |
| 15 | `zh-en-bilingual-subtitle` | custom | — | niche | Generate Chinese ↔ English bilingual subtitles from a single audio / video file. Fully offline (`faster-whisper` ASR + local model). |

> Pipeline #15 is the "niche subtitle" pipeline referenced in `AGENT_GUIDE.md`. Pipeline #2 (`framework-smoke`) is a test-only entry. **The remaining 13 are production pipelines.**

### Decision tree — which pipeline for which request

```
START
  │
  ├── Have a reference / 模板 video? ─────── YES ──→ video-template-remix
  │                                          NO ↓
  │
  ├── Have raw talking-head footage? ──────── YES ──→ talking-head
  │                                          NO ↓
  │
  ├── Have screen recording? ──────────────── YES ──→ screen-demo
  │                                          NO ↓
  │
  ├── Have long-form audio / podcast? ─────── YES ──→ podcast-repurpose
  │   Have a webinar / stream? ───── YES ──→ clip-factory
  │                                          NO ↓
  │
  ├── Need bilingual subtitles only? ─────── YES ──→ zh-en-bilingual-subtitle
  │                                          NO ↓
  │
  ├── Is it documentary / factual / real-world B-roll? ─ YES ──→ documentary-montage
  │                                          NO ↓
  │
  ├── Is it a cartoon character with dialogue? ── YES ──→ character-animation
  │                                          NO ↓
  │
  ├── Is it an avatar / presenter-led sales intro? ─ YES ──→ avatar-spokesperson
  │   (⚠️ but all avatar providers UNAVAILABLE on this host)
  │                                          NO ↓
  │
  ├── Is it an explainer / educational / math / kinetic typography? ─ YES ──→ animation
  │                                          NO ↓
  │
  ├── Is it a fully AI-produced explainer from a topic? ─ YES ──→ animated-explainer
  │                                          NO ↓
  │
  ├── Need localization / dubbing? ─────────── YES ──→ localization-dub
  │                                          NO ↓
  │
  ├── Mix real footage with designed / generated? ── YES ──→ hybrid
  │                                          NO ↓
  │
  └── **Brand film / trailer / hero piece / product marketing? ──→ cinematic** ⭐
```

For a **product-acquisition / 获客 video**, the typical chain is:
- **First pass**: `cinematic` (5–7 scenes, 30 s, I2V from product stills, color-grade `bright_clean` or `clean_professional`, with CTA overlay + subtitle)
- **Second pass (optional)**: take the `cinematic` output as a reference, run `video-template-remix` to mimic pacing of a competitor's viral clip

### Pipeline lock invariants (from `AGENT_GUIDE.md`)

Three decisions are **frozen at proposal stage** and locked for the run:

1. **`render_runtime`** — Remotion / HyperFrames / FFmpeg. Once chosen, **never silently swap**.
2. **`composition_mode`** — `templated` (stock cut.types, fast, generic look) vs `atelier` (hand-authored, expensive, hero-grade). Default `atelier` for hero work.
3. **Provider allowlist** — the per-pipeline `models` / `providers` whitelist is binding.

If a chosen runtime or provider becomes unavailable mid-run, surface a blocker. Do not silently substitute.

---

## Part 2 — Capability Matrix Without External Calls

"Without external calls" = no HTTP to `api.minimaxi.com`, `api.elevenlabs.io`, `api.openai.com`, `api.klingai.com`, `queue.fal.run`, `api.imgbb.com`, `api.kapon.cloud`, `dashscope.aliyuncs.com`, etc. Only `huggingface.co` model fetches are allowed (and only the first time, after which weights are cached at `~/.cache/huggingface/hub/`).

The **single online-bridged** capability we run today — `minimax_h3_kapon` (H3 / H3-Max via kapon OneHub) — is documented separately at the bottom of this doc. Everything below is **offline-capable given current `.env` + cache state**.

### Image generation

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `minimax` (MiniMax image-01) | ❌ unavailable (quota) | No | Needs `MINIMAX_API_KEY` + non-exhausted Token Plan |
| `flux` / `recraft` / `kling` / `gemini` / `jimeng` / `imagen` / `grok` / `dalle` / `qwen_vl` | ❌ unavailable (no key) | No | All require paid API keys not in `.env` |
| `comfyui` | ❌ unavailable (not deployed) | Local-only | Needs self-hosted ComfyUI server |
| **`local` (diffusers)** | ⚠️ conditional | **Yes** if `VIDEO_GEN_LOCAL_ENABLED=true` + GPU | Needs `make install-gpu` (torch / torchaudio / torchvision / diffusers) and an NVIDIA GPU. Models live under `~/.cache/huggingface/hub/`. |

**Currently offline-usable image gen on this host: NONE** — `VIDEO_GEN_LOCAL_ENABLED` is empty in `.env`, no GPU is assumed for general `local` invocation. Add the `local` provider by enabling `VIDEO_GEN_LOCAL_ENABLED=true` and confirming GPU.

### Video generation (excluding the online-bridged `minimax_h3_kapon`)

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `minimax_h3_kapon` (H3 / H3-Max via kapon) | ✅ available | **No** (online-bridged) | See bottom of this doc |
| `minimax_direct` (Hailuo-2.3 via official v1) | ✅ available | No | `MINIMAX_API_KEY`; Token Plan quota ≤4 / cycle |
| `minimax` (Hailuo via fal.ai) | ❌ unavailable | No | `FAL_KEY` empty |
| `kling` / `kling_official` / `kling_relay` | ❌ unavailable | No | No `KLING_API_KEY`, no `FAL_KEY` |
| `veo` / `runway` / `seedance` / `seedance_relay` / `gemini_omni` / `grok` / `higgsfield` | ❌ unavailable | No | All `FAL_KEY`-gated or paid |
| `ltx-modal` | ❌ unavailable | Self-host | `MODAL_LTX2_ENDPOINT_URL` empty |
| **`ltx` / `wan` / `hunyuan` / `cogvideo` (local)** | ⚠️ conditional | **Yes** if `VIDEO_GEN_LOCAL_ENABLED=true` + GPU | Same `local` install gate. Model choices per `.env`: `wan2.1-1.3b`, `wan2.1-14b`, `hunyuan-1.5`, `ltx2-local`, `cogvideo-5b`. |

**Currently offline-usable video gen: NONE.** All online video providers are unavailable; the `local` diffusers path needs GPU + `VIDEO_GEN_LOCAL_ENABLED=true` to switch on.

### Text-to-speech (TTS)

This is the **richest offline capability** — three engines work with on-disk weights only.

| Provider | Status | Offline? | Engine | Quality |
|---|---|---|---|---|
| `kokoro_tts` | ✅ available | **Yes** | `kokoro==0.9.4` + `KPipeline` (`~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/`) | High-quality English + Mandarin (after `pip install 'misaki[zh]'`). 82 M params. Latency ~RTF 0.3 on CPU. |
| `piper_tts` | ✅ available | **Yes** | Piper TTS (C++ binary via `pyenv 3.11.8` `piper-tts` pkg). Voice files: `~/.cache/huggingface/hub/models--rhasspy--piper-voices/` (currently empty — bootstrap needed once with network, then offline forever). | Lower than Kokoro but lighter. |
| `voicebox_tts` | ✅ available | Conditional | Local HTTP service on port 8900. **Currently blocked on this host** (per memory `voicebox-blocked-kokoro-escape-hatch.md`); escape hatch = Kokoro. | When up, voice cloning + TTS. |
| `edge_tts` | ❌ unavailable | **No** | Microsoft Edge TTS over HTTPS — needs network |
| `elevenlabs_tts` | ❌ unavailable | No | `ELEVENLABS_API_KEY` empty |
| `openai_tts` | ✅ available | **No** | `OPENAI_API_KEY` present but call goes to api.openai.com |
| `google_tts` / `dashscope_tts` / `doubao_tts` / `kling_tts` | ❌ unavailable | No | No keys |

**Currently offline-usable TTS: `kokoro_tts`** (and `piper_tts` once voice files are bootstrapped). For Mandarin product-voiceover, Kokoro is the default.

### Music / audio

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `local` (MusicGen-small) | ✅ available | **Yes** | Weights at `~/.cache/huggingface/hub/models--facebook--musicgen-small/`. ~300 MB. CPU OK (~RTF 0.5 for 30 s clips). Run via `make musicgen-fetch`. **License: CC-BY-NC-4.0 — non-commercial use only**; for commercial获客 videos use Pixabay CC0 / ElevenLabs Music instead.
| `acestep` (ACE-Step 1.5) | ⚠️ plugin-installed | **Yes** (model-dependent) | Skill installed; verify model cache |
| `elevenlabs` (Music + Sound Effects) | ❌ unavailable | No | No key |
| `suno` | ❌ unavailable | No | No key |

**Currently offline-usable music: `local` MusicGen-small** (and ACE-Step if its model is cached). BGM generation for product-获客 videos is fully offline-capable **technically**, but **legally** the MusicGen output is `CC-BY-NC-4.0` (non-commercial). For commercial distribution, swap in Pixabay CC0 tracks (already `available`) or pay for ElevenLabs Music. See [`docs/MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) §4 for the reuse recipes and the full license caveat.

### Subtitle / ASR

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `faster_whisper` (default) | ✅ available | **Yes** | CTranslate2 backend. Model `Systran/faster-whisper-base` cached; can also load `large-v3-turbo` (cached). |
| `whisperx` (analysis) | ✅ available | **Yes** | Diarization needs `HF_TOKEN`; otherwise offline |
| `azure_speech` | ❌ unavailable | No | No `AZURE_SPEECH_KEY` |
| `elevenlabs` (Scribe v2) | ❌ unavailable | No | No key |

**Currently offline-usable ASR: `faster_whisper` + `whisperx`.** Subtitle generation from local audio is fully offline-capable.

### Video composition / post / render

| Capability | Provider | Status | Offline? |
|---|---|---|---|
| `video_compose` (filter_complex concat / crossfade / overlay / drawtext / curves) | `ffmpeg` | ✅ available | **Yes** |
| `video_compose` (Remotion React templates) | `remotion` | ✅ available | **Yes** |
| `video_compose` (HTML motion graphic) | `hyperframes` | ✅ available | **Yes** |
| `video_post` (re-encode / extract / trim) | `ffmpeg` / `hyperframes` | ✅ available | **Yes** |
| `burn_subtitles` | (uses ffmpeg + faster_whisper ASR) | ✅ available | **Yes** |
| Remotion 4-template exports (`create_remotion_video_share` etc.) | `remotion` | ✅ available | **Yes** |

**All composition / render is offline.** Final `final_30s_v1.mp4` assembly uses only local ffmpeg + Remotion.

### Analysis

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `transformers` (BLIP2 captioning, etc.) | ✅ available | **Yes** | `Salesforce/blip2-opt-2.7b` cached; CPU 46 s / frame per memory `blip2-opt-2.7b-sharded-safetensors-cache-layout.md` |
| `whisperx` | ✅ available | **Yes** | See above |
| `ffmpeg` / `ffprobe` | ✅ available | **Yes** | Stock binaries |
| `local` (image / video captioning) | ✅ available | **Yes** | Local models |
| `multi` (composite analyzer) | ✅ available | **Yes** | Routes through local providers |
| `anthropic-compatible` | ✅ available | **No** (depends on upstream) | Routes through configured LLM endpoint |
| `azure` / `dashscope` / `funasr` / `mediapipe` / `youtube-transcript-api` | ❌ unavailable | No | No key / dep |

**Most analysis is offline.** Image captioning via BLIP2, video metadata via ffprobe, transcription via WhisperX — all work without network.

### Avatar / character animation

| Provider | Status | Offline? | Notes |
|---|---|---|---|
| `kling_official` avatar | ❌ unavailable | No | No key |
| `sadtalker` / `wav2lip` | ❌ unavailable | Local-only | Need to install + run locally |

**Avatar is currently offline-unavailable** even though the providers are local-first — neither is installed. The `avatar-spokesperson` and `character-animation` pipelines cannot fully run on this host without setup.

### Asset management / delivery

| Capability | Status | Offline? | Notes |
|---|---|---|---|
| `upload_asset` / `upload_asset_chunk` / `read_session_asset` | ✅ available | **Yes** | Local session storage at `projects/<id>/session_assets/` |
| `rsync_upload_artifact` | ✅ available | **Yes** | rsync to local or LAN target |
| `s3_upload` | ⚠️ conditional | **Yes** if `S3_*` env vars filled; **No** otherwise | Currently `S3_ACCESS_KEY=<key>` placeholder → effectively offline-unconfigured |
| `weiyun_upload` / `weiyun_gen_share_link` | ✅ available | **No** (微云网盘 over HTTPS) | Network required |
| `export_bundle` (Remotion self-contained bundle) | ✅ available | **Yes** | Zips local files only |

**Local asset delivery is offline-capable.** Network-bound delivery (微云 / S3 with real keys) is online-only.

---

## Part 3 — Decision Recipes for "Offline-Only Mode"

Below are end-to-end production recipes that run **without any external paid API**. Use these when the host has no outbound network, or when budget is exhausted.

### Recipe A — Product hero video, fully offline

**Pipeline**: `cinematic` with `render_runtime=ffmpeg` + `composition_mode=atelier` (or `templated` if speed matters).

**Stage-by-stage capability mapping**:

| Stage | What you need | Offline-capable provider |
|---|---|---|
| research | Web search for visual refs | ❌ needs `web_search` (online) — **skip or use pre-curated `sources/` folder** |
| proposal | Concept selection | Hand-authored, no network |
| script | Copywriting | Hand-authored |
| scene_plan | Shot list | Hand-authored |
| assets (image gen) | Product stills | **Use supplied PNGs** (`projects/<id>/assets/images/`) — skip image generation |
| assets (video gen) | 5–7 I2V / T2V clips | ⚠️ **T2V impossible offline on this host** (no GPU + no `local` enabled). **I2V impossible offline** (no local I2V model). **Workaround: use supplied video clips** + stock library or hand-shot b-roll. |
| assets (audio / TTS) | Voiceover | `kokoro_tts` ✅ offline |
| assets (music) | BGM | MusicGen-small `local` ✅ offline |
| assets (subtitle ASR) | Subtitle transcription | `faster_whisper` ✅ offline |
| edit | Cut / pace / overlay | `ffmpeg` ✅ offline |
| compose | Final assembly | `ffmpeg` + `Remotion` ✅ offline |
| publish | Delivery | `rsync_upload_artifact` (LAN) ✅ offline |

**Bottleneck**: video generation. Without GPU + `VIDEO_GEN_LOCAL_ENABLED=true` or an external provider, **OM cannot synthesize video offline on this host.** Two paths:

1. **Bring your own video** — drop MP4s into `assets/video/`, skip `minimax_*` tools entirely.
2. **Switch on local diffusers** — `make install-gpu` + `VIDEO_GEN_LOCAL_ENABLED=true` + choose `wan2.1-1.3b` or `ltx2-local`. CPU-only inference exists but is slow (~minutes per 5 s clip). For product 获客 videos, I2V with a still frame is what works best; T2V from text prompts is significantly lower quality on local small models.

### Recipe B — Bilingual subtitle only

**Pipeline**: `zh-en-bilingual-subtitle` (niche pipeline). Fully offline. `faster_whisper` ASR + local NMT. Done.

### Recipe C — Talking-head polish, fully offline

**Pipeline**: `talking-head` with raw footage supplied. Stages:

| Stage | Offline-capable provider |
|---|---|
| Transcribe raw footage | `whisperx` ✅ |
| Generate b-roll | ⚠️ needs `VIDEO_GEN_LOCAL_ENABLED=true` or supplied video |
| Compose | `ffmpeg` ✅ |

If you have raw talking-head footage + a small set of b-roll clips + `faster_whisper`, this runs end-to-end offline.

### Recipe D — Music video / mood reel

Use `cinematic` with `render_runtime=remotion` + `composition_mode=atelier`. Music from MusicGen-small. Visuals from supplied video. Fully offline if you supply the source video.

---

## Part 4 — The One Online-Bridged Capability (Today)

**`minimax_h3_kapon`** — MiniMax-H3 / H3-Max video generation via kapon.cloud OneHub proxy.

- Why it exists: Hailuo Token Plan exhausts after ~4 generations. H3 / H3-Max are not on the official `api.minimaxi.com` v1 endpoint — they live on v2. Until `new-api` routes H3 natively, kapon's Bearer-token proxy is the only available path.
- Cost: H3 768P ¥0.50/s · H3 2K ¥0.80/s · H3-Max 480P ¥0.33/s · H3-Max 768P ¥0.50/s. Independent billing pool from Token Plan.
- Constraints:
  - **Duration 5–15 s** (H3: 4–15 s; H3-Max: 5–15 s — kapon-side hard floor)
  - **I2V requires public HTTPS URL** — kapon runs in datacenter IP ranges; imgbb / public CDNs anti-bot block. We work around with `https://ocbot.aixifs.com/videopic/<file>` nginx reverse-proxied static hosting.
  - **Model allowlist is token-bound** — the kapon Bearer must be granted `MiniMax-H3` / `MiniMax-H3-Max` permission in the kapon console; default tokens cannot call them.
  - **H3-Max does not support** `reference_image` / `reference_video` / `reference_audio` (T2V / I2V / first+last-frame only).
- See `tools/video/minimax_h3_video.py` for the BaseTool, and decision_log `d-009` / `d-010` in `projects/xiaohongshu-neon-night-30s/artifacts/decision_log.json` for the rationale and removal instructions.

---

## Part 5 — Going Offline: Setup Checklist

To run OM end-to-end without any external paid service:

1. **GPU + diffusers** (for video gen):
   ```
   make install-gpu            # installs torch / torchaudio / torchvision / diffusers
   VIDEO_GEN_LOCAL_ENABLED=true  # in .env
   VIDEO_GEN_LOCAL_MODEL=wan2.1-1.3b   # or ltx2-local / cogvideo-5b
   ```
2. **TTS bootstrap** (for Chinese voiceover):
   ```
   .venv/bin/pip install kokoro soundfile 'misaki[zh]'
   .venv/bin/python -c "from kokoro import KPipeline; KPipeline(lang_code='z')"
   ```
3. **Piper voices** (optional lighter TTS):
   ```
   pip install piper-tts
   # bootstrap a voice once with network
   ```
4. **MusicGen weights** (already cached at `~/.cache/huggingface/hub/models--facebook--musicgen-small/`). To re-fetch:
   ```
   make musicgen-fetch
   ```
5. **Whisper model** (already cached). To switch to large-v3-turbo for higher accuracy:
   ```
   export FASTER_WHISPER_MODEL_DIR=~/.cache/huggingface/hub/models--openai--whisper-large-v3-turbo/snapshots/...
   ```
6. **Source media** (b-roll, stock): populate `sources/` with locally-cached Pexels / Pixabay / Archive.org clips. Without `PEXELS_API_KEY` / `PIXABAY_API_KEY`, the doc-montage pipeline cannot pull online.

---

## Part 6 — Quick-Reference Provider Status

Snapshot from `registry.provider_menu_summary()` on 2026-09-10:

| Capability | Available | Unavailable |
|---|---|---|
| analysis | 7 | 5 |
| artifact_delivery | 1 | 0 |
| asset_management | 4 | 0 |
| audio_processing | 2 | 0 |
| avatar | **0** | 4 |
| character_animation | 6 | 0 |
| image_generation | 1 | 11 |
| image_to_video | (overlap with video_generation) | — |
| music_generation | 1 | 4 |
| speech_to_text | 2 | 2 |
| text_to_speech | 5 (3 truly offline) | 5 |
| video_compose | 3 | 0 |
| video_generation | 3 (incl. `minimax_h3_kapon`) | 18 |
| video_post | 2 | 0 |
| video_understanding | 2 | 0 |
| voice_cloning | 1 | 2 |

**Bottom line**: with **no GPU and no paid keys**, OM can deliver
- ✅ Image analysis (BLIP2 captioning), transcription (WhisperX), TTS (Kokoro), music (MusicGen), full video compose / render / subtitle burn
- ❌ Video generation (no local model online, no GPU)
- ❌ Image generation (no local model online)

To restore end-to-end video synthesis without paid keys, enable `VIDEO_GEN_LOCAL_ENABLED=true` and `make install-gpu`. To use paid video gen, `minimax_direct` (Hailuo Token Plan) and `minimax_h3_kapon` (H3 / H3-Max via kapon) are the only two currently routed.
