---
name: voicebox
description: Local, privacy-first text-to-speech and voice cloning via the Voicebox REST API (port 17493 by default). Use when narration must stay on-prem, when voice cloning is needed without a paid cloud subscription, when producing multilingual audio (23 languages), or when ElevenLabs/OpenAI/Edge is unavailable, rate-limited, or too expensive. Triggers include voice cloning, on-device TTS, free TTS, "no API key" TTS, multilingual narration, local LLM-backed voice profiles, or any audio synthesis task where privacy/locality matters more than cloud polish. Also use when the question is "REST vs MCP for voicebox", "how to wire voicebox into OpenMontage", "should voicebox run via :8900 or :17493", "voicebox_speak vs voicebox_tts", or any voicebox integration-path / access-shape decision — the `voicebox` skill is the entry point and the integration paths doc (linked below) owns the path comparison.
---

# Voicebox (Local TTS + Voice Cloning)

Voicebox is OpenMontage's **local-first** TTS provider. It talks REST to a
Voicebox server running on the same host (default `http://127.0.0.1:17493`).
No API key, no cloud spend, no audio leaving the machine. Cloning is a first-class
operation — the model runs on the Voicebox host (Qwen3-TTS, Chatterbox, LuxTTS,
Hume TADA, or Kokoro for preset voices).

## When to Pick Voicebox vs the Cloud Providers

| Need | Pick |
|---|---|
| Voice data must NOT leave the host (regulated, NDA, internal demos) | **voicebox** |
| Free / no per-character cost, willing to install Voicebox locally | **voicebox** |
| Need a specific cloned voice of a real person (no cloud plan) | **voicebox** |
| Multilingual narration across 23 languages incl. low-resource (ar, sw, hi, he) | **voicebox** (Chatterbox Multilingual) |
| Paralinguistic tags like `[laugh] [sigh] [gasp]` | **voicebox** (Chatterbox Turbo) |
| Natural-language delivery control (`instruct`: "speak slowly", "whisper") | **voicebox** (Qwen3-TTS / Qwen CustomVoice) |
| Polished production voice, no infra, willing to spend on credits | ElevenLabs |
| ElevenLabs quota exhausted and Voicebox is offline | OpenAI / Edge / Piper |
| Offline CPU-only host with no Voicebox install and just need any TTS | Piper (no cloning) |

## Tool Operations

The `voicebox_tts` tool exposes three operations:

| Operation | Purpose |
|---|---|
| `text_to_speech` | Synthesize audio from a `profile_id` + `text`. Returns the audio file written under `projects/<id>/assets/audio/`. |
| `clone_voice` | Create a new VoiceProfile and attach 1+ reference samples. Returns `profile_id` for later TTS calls. |
| `list_cloned_voices` | Enumerate VoiceProfiles on the local instance (`voice_type=cloned`). |

Default operation: if a caller passes only `text`, it routes to `text_to_speech`.

## Voice Cloning Flow (REST)

Cloning is a two-step REST dance:

```
POST /profiles
   { name, language, voice_type: "cloned", default_engine, description? }
   → { id: <profile_id>, ... }

POST /profiles/{profile_id}/samples        (multipart)
   file=<audio bytes>   reference_text=<transcript?>
   (repeat for every reference sample)

# Then use the returned profile_id for synthesis:
POST /generate     { profile_id, text, language, engine }
```

**Sample requirements:**

- **Duration**: ≥30s recommended (Qwen3-TTS yields a usable clone; shorter
  clips succeed but sound thin). Two or three clips outperform one long one.
- **Formats accepted**: `wav`, `mp3`, `m4a`, `ogg`, `flac`, `aac`, `webm`, `opus`.
- **Optional transcripts**: `reference_texts` (1:1 with `audio_paths`) or a
  single `reference_text` applied to every sample. Better transcripts →
  better clone fidelity.
- **Engine compatibility**: only `qwen`, `luxtts`, `chatterbox`,
  `chatterbox_turbo`, `tada` accept cloned samples. `kokoro` and
  `qwen_custom_voice` are preset-only and ignore any samples you upload.

## TTS Flow (REST)

```
POST /generate
   { profile_id, text, language, engine?, model_size?, instruct?, seed?, personality? }
   → { id: <generation_id>, status: "queued" }

GET  /generate/{generation_id}/status     (SSE)
   data: { id, status: "queued"|"generating"|"completed"|"failed",
           duration?, error?, source? }
   ← poll until status=completed (default poll cadence: 1s)

GET  /audio/{generation_id}                (binary)
   → writes to projects/<id>/assets/audio/voicebox_{gen_id}.{ext}
```

Generation is **async**. The tool opens the SSE stream, reads terminal events,
and bounds the wait by `timeout_seconds` (default **600s**). A stuck worker
fails the call instead of hanging the pipeline.

## Engine Selection Matrix

| Engine | Clones? | Langs | Strengths | Tradeoffs |
|---|---|---|---|---|
| `qwen` (Qwen3-TTS 0.6B / 1.7B) | yes | 23 | Best multilingual cloning quality; supports `instruct` ("speak slowly", "whisper") | 1.7B is VRAM-hungry; 0.6B is the safe default |
| `qwen_custom_voice` | no (preset) | 10+ | 9 preset voices, natural-language delivery, no reference audio | Pick by preset id, not by clone |
| `luxtts` | yes | en only | Lightweight, runs on CPU at ~150× realtime | English only, fewer voice nuances |
| `chatterbox` | yes | 23 | Broadest 23-language coverage — Arabic, Swahili, Hindi, Hebrew, etc. | Higher latency than `chatterbox_turbo` |
| `chatterbox_turbo` | yes | en | Fast 350M model; paralinguistic tags `[laugh] [sigh] [gasp]` | English only |
| `tada` (HumeAI) | yes | en | Emotive / expressive speech | Em-dramatic; pick when energy matters more than neutrality |
| `kokoro` | no (preset) | en + others | 50+ preset voices, lightweight CPU | Preset only — pass `profile_id` for a preset profile or supply `engine=kokoro` for the default voice |

**Default choice if unsure:** `qwen` with `model_size=0.6B` — best quality /
cost / latency tradeoff and works for any cloned English voice.

## Language Enum

Voicebox validates `language` against this whitelist per engine. Anything else
gets HTTP 400.

```
zh  en  ja  ko  de  fr  ru  pt  es  it
he  ar  da  el  fi  hi  ms  nl  no  pl
sv  sw  tr
```

23 codes total. For broader coverage, fall back to ElevenLabs multilingual_v2
or OpenAI TTS.

## Fallback Chain

```
voicebox_tts  →  elevenlabs_tts  →  openai_tts  →  piper_tts
   (local, free)    (cloud, $$)      (cloud, $)     (offline, no clone)
```

- **voicebox_tts** is the default when Voicebox is reachable (`/health` 200).
- **elevenlabs_tts** is the cloud fallback when Voicebox is offline / degraded.
- **openai_tts** is the backup if ElevenLabs quota is exhausted.
- **piper_tts** is the last-resort fully-offline provider; it does **not** support voice cloning — fall back to a preset voice in this case.

The tool's `fallback` / `fallback_tools` fields declare the chain
(`fallback="elevenlabs_tts"`, `fallback_tools=["elevenlabs_tts", "piper_tts"]`).

## Gotchas

1. **Voicebox must be running locally.** Check `GET {base}/health` — 200 = up,
   502/503/504 = degraded (e.g. model still loading), unreachable = unavailable.
   The tool's `get_status()` reflects this so `make preflight` shows the right state.
2. **REST URL is `VOICEBOX_REST_URL`** (default `http://127.0.0.1:17493`). The
   OpenMontage MCP server at :8900 reverse-proxies `/voicebox/mcp/*` to the same
   port; this tool talks REST directly so pipelines don't need to go through MCP.
3. **`X-Voicebox-Client-Id` header is mandatory.** Even loopback callers must
   send it (`openmontage-tts`). Voicebox's middleware uses it for per-client
   policies (audio_path gating, default voice bindings).
4. **Generation is async — poll with `timeout_seconds`.** Default 600s. CPU-only
   hosts running long scripts may need to bump this via the tool input or
   `DEFAULT_GENERATION_TIMEOUT_S`.
5. **GPU memory matters for `qwen 1.7B`.** ~4 GB VRAM minimum. On a CPU-only or
   low-VRAM host, prefer `qwen 0.6B`, `luxtts`, or `chatterbox_turbo`.
6. **Output must land under `projects/<id>/assets/audio/`.** The Backlot board
   only watches files in `projects/*/`. Pass `output_path` explicitly or let
   `infer_project_dir()` resolve the active project.
7. **Errors are HTTP 400 with a `detail` field** for regex/validation failures
   (bad engine, bad language, missing field). The tool passes `detail` through
   verbatim — fix the offending field rather than guessing.
8. **Incomplete clone = stranded profile.** A failed sample upload leaves the
   profile half-populated. The tool surfaces `failed_samples` so the agent can
   retry the upload or `DELETE /profiles/{id}` to clean up.
9. **`personality=true` triggers voicebox's bundled local LLM** to rewrite the
   text in-character before TTS — slow on CPU. Leave `false` unless the profile
   has a personality prompt and you actually want in-character delivery.

## Installation

```bash
# macOS / Windows: install the Voicebox desktop app from voicebox.sh
# Docker: clone the Voicebox repo and `docker compose up`
# Then confirm it's reachable:
curl http://127.0.0.1:17493/health
# Optional: point at a remote host
export VOICEBOX_REST_URL=http://voicebox.internal:17493
```

For full API detail (every endpoint, request/response shape, status codes,
curl examples), see [reference.md](reference.md).

## See Also

- [`docs/voicebox-integration-paths.md`](../../../docs/voicebox-integration-paths.md) — **REST vs MCP integration paths (REST A / MCP wrapper B1 / MCP reverse-proxy B2)**, decision table by caller, failure modes, performance tradeoffs. **Read this first** when the question is "how do I integrate voicebox" or "which path should I pick" rather than "what is voicebox".
- [`docs/voicebox-prerequisites.md`](../../../docs/voicebox-prerequisites.md) — HF proxy, model weights, cache layout. Required regardless of integration path.
- [`docs/voicebox-installation-pitfalls.md`](../../../docs/voicebox-installation-pitfalls.md) — Common installer footguns.
- [`docs/openmontage-integration.md`](../../../docs/openmontage-integration.md) — *Direction* (who calls whom): Direction A/B/C. Complementary to the integration-paths doc, which covers *access shape*.