# Voicebox REST API Reference

> **Before reading endpoints:** [`docs/voicebox-integration-paths.md`](../../../docs/voicebox-integration-paths.md) covers **REST vs MCP access paths** (which port / which entry point to use). This reference only covers the REST contract (`http://127.0.0.1:17493`); for MCP wrappers and the OpenMontage :8900 reverse-proxy see the integration-paths doc.

Endpoint detail for the local Voicebox server (`http://127.0.0.1:17493` by
default; override with `VOICEBOX_REST_URL`). Every request must include the
`X-Voicebox-Client-Id` header (the OpenMontage tool sends `openmontage-tts`).

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness probe — 200 = up, 502/503/504 = degraded. |
| `/profiles` | GET | List all VoiceProfiles on this instance. |
| `/profiles` | POST | Create a new VoiceProfile (empty, no samples yet). |
| `/profiles/{id}` | DELETE | Delete a profile and its samples. |
| `/profiles/{id}/samples` | POST (multipart) | Attach a reference audio sample + transcript. |
| `/generate` | POST | Kick off async TTS synthesis; returns `{id, status}`. |
| `/generate/{id}/status` | GET (SSE) | Stream synthesis status events until terminal. |
| `/audio/{generation_id}` | GET | Download the synthesized audio bytes. |

---

## `GET /health`

```bash
curl -s http://127.0.0.1:17493/health
```

| Code | Meaning |
|---|---|
| 200 | Voicebox is healthy — TTS available |
| 502 / 503 / 504 | Degraded — model still loading or worker transient |

---

## `POST /profiles`

Create an empty VoiceProfile. Step 1 of the cloning flow.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Unique display name |
| `language` | string | yes | One of the 23 enum codes (see SKILL.md) |
| `voice_type` | string | yes | `"cloned"` for cloning, `"preset"` for system voices |
| `default_engine` | string | yes | Must be a cloning engine: `qwen`, `luxtts`, `chatterbox`, `chatterbox_turbo`, `tada` |
| `description` | string | no | Free-text notes |

```bash
curl -s -X POST http://127.0.0.1:17493/profiles \
  -H 'Content-Type: application/json' \
  -H 'X-Voicebox-Client-Id: openmontage-tts' \
  -d '{
    "name": "Narrator v1",
    "language": "en",
    "voice_type": "cloned",
    "default_engine": "qwen",
    "description": "Documentary narrator"
  }'
```

**Response 200:**

```json
{
  "id": "prof_abc123",
  "name": "Narrator v1",
  "language": "en",
  "voice_type": "cloned",
  "default_engine": "qwen",
  "description": "Documentary narrator",
  "created_at": "2026-08-21T12:00:00Z"
}
```

| Code | Meaning |
|---|---|
| 200 | Created |
| 400 | Validation error (`detail` field explains which field) |
| 409 | Profile name already exists on this instance |

---

## `POST /profiles/{profile_id}/samples`

Attach a reference audio sample to a profile. Multipart upload. Step 2 of the cloning flow — call once per sample.

**Multipart fields:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | One of: `.wav .mp3 .m4a .ogg .flac .aac .webm .opus` |
| `reference_text` | string | no | Transcript of this sample (improves clone fidelity) |

```bash
curl -s -X POST "http://127.0.0.1:17493/profiles/prof_abc123/samples" \
  -H 'X-Voicebox-Client-Id: openmontage-tts' \
  -F 'file=@sample1.wav;type=audio/wav' \
  -F 'reference_text=Welcome to the show.'
```

| Code | Meaning |
|---|---|
| 200 | Sample attached |
| 400 | Unsupported extension or malformed upload |
| 404 | `profile_id` does not exist |

---

## `GET /profiles`

List all VoiceProfiles. Cloned profiles carry `"voice_type": "cloned"` and
`"is_cloned": true`.

```bash
curl -s http://127.0.0.1:17493/profiles \
  -H 'X-Voicebox-Client-Id: openmontage-tts'
```

| Code | Meaning |
|---|---|
| 200 | Array of profiles |
| 503 | Backend model registry unavailable |

---

## `DELETE /profiles/{profile_id}`

```bash
curl -s -X DELETE http://127.0.0.1:17493/profiles/prof_abc123 \
  -H 'X-Voicebox-Client-Id: openmontage-tts'
```

| Code | Meaning |
|---|---|
| 204 | Deleted |
| 404 | Profile not found |

---

## `POST /generate`

Kick off async synthesis. Returns a `generation_id` to poll.

**Request body:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `profile_id` | string | yes | From `POST /profiles` or `GET /profiles` |
| `text` | string | yes | What to speak |
| `language` | string | yes | One of the 23 enum codes |
| `engine` | string | no | One of: `qwen`, `qwen_custom_voice`, `luxtts`, `chatterbox`, `chatterbox_turbo`, `tada`, `kokoro` |
| `model_size` | string | no | Engine-specific (e.g. `0.6B` or `1.7B` for Qwen3-TTS) |
| `instruct` | string | no | Natural-language delivery hint (Qwen only) |
| `seed` | integer | no | Reproducibility (engine-dependent) |
| `personality` | bool | no | Rewrite text in-character via local LLM before TTS |

```bash
curl -s -X POST http://127.0.0.1:17493/generate \
  -H 'Content-Type: application/json' \
  -H 'X-Voicebox-Client-Id: openmontage-tts' \
  -d '{
    "profile_id": "prof_abc123",
    "text": "Welcome to the show.",
    "language": "en",
    "engine": "qwen",
    "model_size": "0.6B",
    "instruct": "speak calmly and warmly"
  }'
```

**Response 200:**

```json
{
  "id": "gen_xyz789",
  "status": "queued"
}
```

| Code | Meaning |
|---|---|
| 200 | Generation accepted |
| 400 | Invalid `engine`, `language`, or other field — see `detail` |
| 404 | `profile_id` not found |
| 502 / 503 | Model still loading or worker overloaded |

---

## `GET /generate/{generation_id}/status` (SSE)

Server-Sent Events stream of synthesis status. Connect with `Accept: text/event-stream` and read until terminal status (`completed` or `failed`).

**Event payload (one JSON object per `data:` line):**

| Field | Type | Notes |
|---|---|---|
| `id` | string | Echoes the `generation_id` |
| `status` | string | `queued` → `generating` → `completed` / `failed` |
| `duration` | float | Seconds of generated audio (only on `completed`) |
| `error` | string | Failure reason (only on `failed`) |
| `source` | string | Engine identifier (e.g. `qwen`, `chatterbox`) |

```bash
curl -N http://127.0.0.1:17493/generate/gen_xyz789/status \
  -H 'Accept: text/event-stream' \
  -H 'X-Voicebox-Client-Id: openmontage-tts'

# data: {"id":"gen_xyz789","status":"queued"}
# data: {"id":"gen_xyz789","status":"generating"}
# data: {"id":"gen_xyz789","status":"completed","duration":3.42,"source":"qwen"}
```

| Code | Meaning |
|---|---|
| 200 | SSE stream open (close after terminal event) |
| 404 | `generation_id` not found |
| 504 | Worker timeout mid-stream |

---

## `GET /audio/{generation_id}`

Download the synthesized audio as a binary stream. The Content-Disposition
header carries the filename + extension (typically `.wav`).

```bash
curl -s -o out.wav http://127.0.0.1:17493/audio/gen_xyz789 \
  -H 'X-Voicebox-Client-Id: openmontage-tts'
```

| Code | Meaning |
|---|---|
| 200 | Audio bytes (FastAPI `FileResponse`) |
| 404 | `generation_id` not found or audio was garbage-collected |
| 409 | Generation hasn't finished yet — poll `/status` first |

---

## Error Responses

Voicebox returns standard FastAPI error envelopes:

```json
{ "detail": "invalid engine 'foo'; expected one of: qwen, ..." }
```

| Code | When |
|---|---|
| 400 | Regex / enum validation failure (engine, language, missing field) |
| 401 | Missing `X-Voicebox-Client-Id` header |
| 404 | Resource (`profile_id`, `generation_id`) not found |
| 409 | Conflict (duplicate profile name, audio not yet ready) |
| 502 / 503 / 504 | Backend model still loading or worker transient |
| 422 | Request body shape invalid |

---

## Engines at a Glance

| Engine ID | Clones? | Langs | Notes |
|---|---|---|---|
| `qwen` | yes | 23 | Best multilingual cloning; supports `instruct` |
| `qwen_custom_voice` | no | 10+ | 9 preset voices, natural-language delivery |
| `luxtts` | yes | en | CPU 150× realtime, English only |
| `chatterbox` | yes | 23 | Broadest coverage incl. `ar`, `sw`, `hi`, `he` |
| `chatterbox_turbo` | yes | en | 350M fast model; `[laugh]` `[sigh]` `[gasp]` tags |
| `tada` | yes | en | HumeAI TADA — emotive |
| `kokoro` | no | en + others | 50+ preset voices, lightweight CPU |

## Languages (23-code enum)

```
zh  en  ja  ko  de  fr  ru  pt  es  it
he  ar  da  el  fi  hi  ms  nl  no  pl
sv  sw  tr
```

## Accepted Audio Extensions

`.wav .mp3 .m4a .ogg .flac .aac .webm .opus`