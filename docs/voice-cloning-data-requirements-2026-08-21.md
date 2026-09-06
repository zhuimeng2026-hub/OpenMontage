# Voice Cloning — Data Requirements Cheat Sheet (2026-08-21)

> Frozen reference card. Re-read before recording reference samples for a
> voice-clone job on this host (Voicebox REST at :17493, reached via the
> `clone_voice` MCP wrapper which routes to `voicebox_tts`).
>
> Source of truth for "what works on this machine":
>   - Engine roster: `tools/audio/voicebox_tts.py::SUPPORTED_ENGINES`
>   - Per-tool defaults: `tools/audio/voicebox_tts.py::CLONING_ENGINES`
>   - Underlying engines: Qwen3-TTS (qwen), LuxTTS (luxtts), Chatterbox
>     (chatterbox, chatterbox_turbo), TADA (tada).

## The One-Page Summary

| Tier | Total audio | Sample shape | Quality | When to use |
|---|---|---|---|---|
| **A. Works** | ≥ 30 s | 1 short clip (10-30 s) | Recognizable as "that voice"; mild machine flavor | Validate the pipeline; throwaway PoC |
| **B. Production** | 1-3 min | 3-5 clips, **15-30 s each** | Tonal match + natural prosody; 90 % of uses covered | Real narration, social clips |
| **C. Hi-fi** | 5-10 min | 8-15 clips, **15-30 s each**, varied prosody | Hard to to hard to tell from real speaker | Hero voice for long-form / commercial |

Hard floors and ceilings:

- **Each sample: 10-30 seconds.** Voicebox rejects samples over 30 s
  (`400 Audio too long (maximum 30.0 seconds)`). Anything under 10 s is
  technically accepted but yields a thin, "machine-flavored" clone.
- **Total: ≥ 30 s** is the practical floor for "clone-like" output.
  Total 1-3 min is the production sweet spot.
- **5 s** will technically run but is marketing-tier — output sounds like the
  speaker with a head cold.

> **Update 2026-08-21:** The earlier version of this doc said "30 s total"
> was the per-sample minimum. That was wrong — Voicebox enforces
> **30 s per sample as a MAXIMUM**, not a minimum. The previous text has
> been corrected and the per-engine table clarified to mean per-sample
> duration.

## Per-Engine Sweet Spots (this Voicebox instance)

> All durations in this table are **per sample**. Voicebox caps each
> uploaded file at 30 s, so "30-60 s" means "each clip is 30-60 s" — you'd
> send 2-3 such clips as separate `audio_paths` entries. Total dataset
> follows the tier table above.

Default is `qwen` (Qwen3-TTS instant clone). Switch via the `engine` arg.

| Engine | Per-sample min viable | Per-sample sweet spot | Personality |
|---|---|---|---|
| **qwen** | 3-10 s | 15-30 s | Fastest convergence on small datasets; 16 kHz clean |
| **chatterbox** | 10-20 s | 20-30 s | Most tolerant of background noise; good for "phone interview" sources |
| **chatterbox_turbo** | 10-20 s | 20-30 s | Same as chatterbox, faster inference |
| **luxtts** | 20-30 s | 25-30 s | Neural concatenative — needs more (per-sample) data to stabilize |
| **tada** | 10-30 s | 20-30 s | Newer; behavior still settling, but competitive on 1 min+ total |
| **qwen_custom_voice** | n/a | n/a | **Does NOT support cloning** (preset voices only) |

If unsure: stay on `engine="qwen"` until you have a reason to switch.

## Quality Six (More Important Than Duration)

Quantity is half. Quality of the samples matters more.

1. **Single speaker per profile** — mixing speakers teaches the model
   nothing well. If your source has multiple speakers, segment first.
2. **Clean signal** — no BGM, no echo, no mains hum. Phone-speaker-in-a-room
   is the worst common failure mode.
3. **Prosodic variety** — monotone "reading-aloud" reads worse than natural
   speech with varied pace and emphasis. A 30 s clip of someone explaining
   their day beats 60 s of straight news anchoring.
4. **Complete sentences** — don't cut mid-thought. Use natural boundaries
   (clause ends, breaths).
5. **Consistent capture** — same mic or headset throughout. Switching from
   MacBook built-in to USB midway is audible to the model.
6. **Accurate `reference_texts`** — must be verbatim what's spoken, in order.
   Missing words, added words, or out-of-order transcripts bias the clone
   back toward the engine's default voice.

## Concrete Recipe (Chinese narration, ~165 s total, 5 samples)

```
audio_paths[0]:  intro_25s.wav      reference_texts[0]: "开场白逐字内容"
audio_paths[1]:  main_30s.wav       reference_texts[1]: "主体内容逐字"
audio_paths[2]:  emotion_25s.wav    reference_texts[2]: "带情绪的句子"
audio_paths[3]:  rapid_25s.wav      reference_texts[3]: "语速偏快的句子"
audio_paths[4]:  slow_25s.wav       reference_texts[4]: "语速偏慢的句子"
                 → 130 s total, 5 samples (each ≤ 30 s)
                 → engine="qwen" (default)
```

If you can't record 5 samples, **3 samples of 20-30 s each** is the next
tier down — still covers pace variety. **Never** ship one sample under
10 s unless the deliverable is a joke or a test, and **never** send a
sample longer than 30 s — Voicebox will 400 it before reaching the
model.

## Call Signature (MCP :8900)

```json
{
  "method": "tools/call",
  "params": {
    "name": "clone_voice",
    "arguments": {
      "name": "my-narrator-zh",
      "audio_paths": ["/abs/intro.wav", "/abs/main.wav", "/abs/emotion.wav"],
      "reference_texts": [
        "intro transcript verbatim",
        "main transcript verbatim",
        "emotion transcript verbatim"
      ],
      "engine": "qwen",
      "description": "中文旁白声,3 样本 ~120 s"
    }
  }
}
```

Both `clone_voice` and the longer-named `voicebox_clone_voice` accept the
same shape. Reference text is **required** — Voicebox returns
`422 reference_text required` on missing fields.

## Reality Check

Marketing claims of "5-second voice clone" are technically true but
practically misleading. On this Voicebox, here's what to expect by tier:

-  Tier A (10 s, 1 sample): useful for pipeline tests; will not survive
  a 30-second listen by anyone paying attention.
-  Tier B (1-3 min, 3-5 samples): indistinguishable from a tired or
  slightly distracted speaker. Production-OK for most narration.
-  Tier C (5+ min, 8-15 samples): you stop noticing it's synthetic. But
  this tier demands **studio-quality** recording — Tier C with office-mic
  audio underperforms Tier B with clean audio.

If the source audio was recorded on a phone in a meeting room, Tier B
is your realistic ceiling regardless of how much you record.

## Where This Lives in the Code

- MCP wrapper: `mcp_server.py::clone_voice` (or `voicebox_clone_voice`,
  identical contract).
- Underlying tool: `tools/audio/voicebox_tts.py::_clone_voice` and
  `_list_cloned_voices`.
- Sample-cleanup recipe: `DELETE /profiles/{id}` against
  `http://127.0.0.1:17493/profiles/{id}`.

## Reproduction

```bash
# Tier-A smoke (1 × 5 s sample, just to verify pipeline):
TOKEN=$(grep ^MCP_API_TOKEN= .env | cut -d= -f2-)
# ... generate sample via edge_tts, then:
curl -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"clone_voice",
                 "arguments":{"name":"smoke",
                              "audio_paths":["/tmp/sample.wav"],
                              "reference_texts":["transcript here"],
                              "engine":"qwen"}}}'

# Cleanup:
curl -X DELETE "http://127.0.0.1:17493/profiles/<id-from-response>"
```