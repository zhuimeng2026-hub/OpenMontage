# docs/ — Index

> Pointer file. Every entry below lives under `docs/`.

## Core references (read first)

- [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — **All 14 production pipelines with usage + decision tree; OM's full capability matrix split into "needs external paid API" vs "fully offline-capable on this host"**. Start here when picking a pipeline or planning offline work.
- [`MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) — **Audio production gotchas**: Kokoro 40s warm-up per call, MusicGen critical slowness on CPU + offline-mode fix, WhisperX base model homophone errors, license traps for commercial获客 videos, plus the recipe to reuse the already-generated `/tmp/musicgen_test.wav`.
- [`RESOURCES.md`](RESOURCES.md) — **Canonical handoff index of all available assets on 2026-09-10** (4 Pixabay BGM candidates with hashes, MusicGen outputs, TTS tests, final mp4, recipe to build the office-demo version with `pixabay_happy.mp3` as primary BGM pick). Point another LLM at this file when scripting a new获客 video.
- [`PROMPT-FOR-SCRIPT-LLM.md`](PROMPT-FOR-SCRIPT-LLM.md) — **Copy-paste prompt template to send to the script LLM** + complete output JSON schema + hard constraints + failure-mode pre-avoidance + worked example. Pair with `RESOURCES.md` for the canonical "generate a获客 video script" workflow.
- [`VIDEO-GEN-SMOKE-CHECKLIST.md`](VIDEO-GEN-SMOKE-CHECKLIST.md) — **6-probe pre-flight checklist for kapon.cloud OneHub I2V** (`minimax_h3_video` BaseTool). Verifies token alive, model allowlist (H3 + H3-Max), all 3 I2V format variants, anti-bot URL source (ocbot mirror vs imgbb vs data URI). Run before any new I2V session. Verified against live kapon 2026-09-10.
- [`SUBTITLE-VOICEOVER-ALIGNMENT.md`](SUBTITLE-VOICEOVER-ALIGNMENT.md) — **How to align SRT subtitles to actual TTS voiceover timing** (Kokoro + WhisperX). Includes why section-boundary-aligned subtitles drift ~25s, complete code template, hand-split heuristics for merged ASR segments, libass vs drawtext comparison.
- [`PROVIDERS.md`](PROVIDERS.md) — every paid provider with setup, pricing, free-tier notes.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — full technical reference (decision log, schema internals).
- [`PR_REVIEW_GUIDE.md`](PR_REVIEW_GUIDE.md) — review checklist for landing changes.

## Operational guides

- [`tweak-server.md`](tweak-server.md) — end-user render-tweak sidecar protocol.
- [`single-port-arch.md`](single-port-arch.md) — `:8900` vclaw-fronted topology, SSE streaming fix, auth split.

## Project / production logs

- `projects/`-specific notes live in each project's `events.jsonl`, `decision_log.json`, and per-session handoff files. See also `../HANDOFF-2026-09-10.md` for the most recent cross-session handoff.

## Test plans / QA

- `../QA_PLAN.md` — QA plan referenced from project CLAUDE.md; consult before adding tests under `tests/qa/`.
