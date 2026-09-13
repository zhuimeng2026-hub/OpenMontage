# Whisper (faster-whisper) — Availability Assessment (2026-08-28)

> Frozen verdict. **Whisper is usable on this host today.** Re-run the
> reproduction script in §4 before re-asserting "whisper is unavailable" in
> any diagnosis. Last assessed by Claude (MiniMax-M3) on 2026-08-28 in
> response to recurring LLM misreads. Authoritative source:
> [`tools/analysis/transcriber.py`](../tools/analysis/transcriber.py).

## TL;DR for the next LLM

| Question | Answer |
|---|---|
| Is `faster-whisper` installed? | Yes — `faster_whisper 1.2.1` in `.venv` (Python 3.10.12) |
| Is the default model on disk? | Yes — `Systran/faster-whisper-base`, snapshot `ebe41f70…`, 145 MB `model.bin` |
| Does `tools.tool_registry` report it AVAILABLE? | Yes — see §3 |
| Can it actually transcribe an audio file? | Yes — end-to-end smoke test in §4 returns 1 segment, 13 word timestamps, transcript JSON on disk |
| Does it need network? | **No.** The transcriber resolves the model to a local snapshot path before instantiation; faster-whisper's `WhisperModel(<abs path>)` short-circuits the hub (see `tools/analysis/transcriber.py:1-13` and the `_snapshot_path` resolution at line 101) |
| Is GPU used? | No — `torch.cuda.is_available()` is `False`; runs CPU `int8` (fast enough: 5 s audio → 3.8 s wall) |
| Are other models available? | Yes — `models--openai--whisper-large-v3-turbo` is also cached; pass `model_size="turbo"` |
| Is diarization (`whisperx`) available? | **No** — `whisperx` not installed, `HF_TOKEN` not set. `diarize=True` silently degrades to no-speaker-labels |

If you (next LLM) are about to write "whisper is unavailable on this host,"
stop, read §1 first.

## 1. Why prior LLMs got this wrong

Three concrete failure modes have shown up in past sessions on this repo:

1. **"No internet → no model"** — `huggingface.co` is unreachable from this
   host (`urllib.request.urlopen("https://huggingface.co/...")` raises
   `URLError: Network is unreachable`). A naïve check concludes that any
   HuggingFace-backed tool must be broken. False: faster-whisper can load
   from an absolute local snapshot directory; `tools/analysis/transcriber.py`
   resolves to that path explicitly (lines 274–296 of that file) precisely
   to defeat this assumption. Introduced in commit
   `110d5a5 feat(tools): make faster_whisper transcriber actually usable offline`.
2. **"Package imports → AVAILABLE" without checking weights** — the
   historical `get_status()` returned `AVAILABLE` whenever `import
   faster_whisper` succeeded, even when no model snapshot existed on disk.
   This is the same class of bug that bit `piper_tts` and `kokoro_tts`
   (see `docs/tts-local-alternatives-assessment-2026-08-21.md`). The fix
   is in the same commit (`110d5a5`): `get_status()` now also requires a
   usable snapshot at `~/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/<sha>/`
   containing `model.bin` + `config.json` + `tokenizer.json`. If the
   snapshot is missing the status is `DEGRADED` instead of `AVAILABLE`.
3. **"No CUDA → cannot run"** — CUDA is indeed not available
   (`torch 2.13.0+cu130`, `torch.cuda.is_available() == False`). But
   faster-whisper runs CPU `int8` and is fast enough for the audio this
   pipeline produces (clip-level narration, podcast segments). Don't
   gate "available" on CUDA.

The correct check order is **install → weights → registry → smoke test**.
Any verdict based only on (1) or (3) is unreliable.

## 2. Static evidence (recorded 2026-08-28)

```text
[OK] faster_whisper installed
     version: 1.2.1
[INFO] HF cache root: /root/.cache/huggingface/hub
[INFO] default repo: Systran/faster-whisper-base
[INFO] usable snapshot: /root/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66
     OK model.bin     (145217532 bytes)
     OK config.json   (2309 bytes)
     OK tokenizer.json (2203239 bytes)
[INFO] cached whisper-family repos on this host:
       - models--Systran--faster-whisper-base            (default)
       - models--openai--whisper-large-v3-turbo          (turbo/large-v3)
[INFO] torch: 2.13.0+cu130  cuda: False
[INFO] whisperx NOT installed  -> diarization disabled
[INFO] HF_TOKEN set: False  -> pyannote diarization would fail even if whisperx were installed
[NET] FAIL https://huggingface.co/api/models/Systran/faster-whisper-base   URLError [Errno 101] Network is unreachable
[NET] FAIL https://www.google.com                                          URLError [Errno 101] Network is unreachable
```

Note the `FASTER_WHISPER_MODEL_DIR` override in `.env` is unset
(comment-only); precedence falls through to the default HF cache
(`~/.cache/huggingface/hub`). If a future deploy moves the cache, this
doc's exact paths go stale but the reproduction script in §4 still works
because it re-resolves from env.

## 3. Registry evidence

```text
transcriber  provider=whisperx  tier=core  stability=experimental
status: AVAILABLE
capabilities: ['transcribe', 'word_timestamps', 'diarization', 'language_detection']
dependencies: ['python:faster_whisper']
agent_skills:  ['speech-to-text']
```

Note the field is `ToolStatus.AVAILABLE` (the enum), not the literal string
`"available"`. If `get_status()` returns `DEGRADED`, the snapshot is
missing — re-run the install snippet in §5, not the smoke test.

## 4. End-to-end reproduction script

This is the canonical proof. Run it before claiming whisper is broken.

```bash
cd /opt/OpenMontage_Voicebox
source .venv/bin/activate
python - <<'PY'
"""Whisper availability smoke test — frozen 2026-08-28.

Verifies: faster_whisper importable, default snapshot usable,
registry reports AVAILABLE, end-to-end transcribe on real audio
emits non-empty segments + word_timestamps + transcript JSON.

Idempotent. Writes to /tmp/om_whisper_smoke/. No network calls.
"""
import sys, time, traceback
from pathlib import Path

# 1. Pick an audio sample (any wav/mp3/m4a under projects/).
candidates = []
for ext in ("wav", "mp3", "m4a"):
    candidates += list(Path("projects").rglob(f"*.{ext}"))
if not candidates:
    print("[FAIL] no audio sample under projects/; abort"); sys.exit(2)
sample = candidates[0]
print(f"[INFO] sample: {sample}")

# 2. Static preconditions.
from tools.analysis.transcriber import (
    _resolve_cache_root, _snapshot_path, _USABLE_FILES,
    DEFAULT_MODEL_REPO,
)
root = _resolve_cache_root()
snap = _snapshot_path(root, DEFAULT_MODEL_REPO)
assert snap is not None, f"default snapshot missing in {root} — see §5"
for f in _USABLE_FILES:
    assert (snap / f).is_file(), f"missing {f} in {snap}"

# 3. Registry status.
from tools.tool_registry import registry
registry.discover()
t = registry._tools["transcriber"]
assert t.get_status().name == "AVAILABLE", \
    f"registry says {t.get_status().name} — fix install before continuing"

# 4. Real transcribe.
out = Path("/tmp/om_whisper_smoke"); out.mkdir(exist_ok=True)
t0 = time.time()
res = t.execute({
    "input_path": str(sample),
    "model_size": "base",
    "language": "en",
    "output_dir": str(out),
})
elapsed = time.time() - t0
assert res.success, f"execute failed: {res.error}"
assert res.data["segments"], "no segments returned"
assert res.data["word_timestamps"], "no word timestamps"
print(f"[OK] {elapsed:.2f}s  lang={res.data['language']}  "
      f"segments={len(res.data['segments'])}  "
      f"words={len(res.data['word_timestamps'])}  "
      f"device={res.data['device']}  "
      f"artifact={res.artifacts[0]}")
PY
```

Expected output on a healthy host (this is the exact transcript that
was captured during the 2026-08-28 assessment, on
`projects/the-refactor-serenade/assets/audio/chorus.wav`):

```text
[INFO] sample: projects/the-refactor-serenade/assets/audio/chorus.wav
[OK]   3.80s  lang=en  segments=1  words=13  device=cpu  artifact=/tmp/om_whisper_smoke/chorus_transcript.json
```

The 3.80 s is **first-call** wall time and includes `WhisperModel(...)`
cold-load from disk. Subsequent calls against an already-cached model
drop to ~1.3 s for the same 5 s clip (OS page cache warm). Both numbers
are fine; what matters is `segments>=1 && words>=1`.

If this prints `[OK]` with `segments>=1`, **whisper is usable, full stop.**
Do not write "whisper is unavailable" in any downstream analysis based on
this evidence.

## 5. Install snippet (only needed if the snapshot is missing)

```bash
cd /opt/OpenMontage_Voicebox
source .venv/bin/activate
pip install 'faster-whisper[gpu]'   # or `faster-whisper` for CPU-only
# One-time weight fetch. Run on a host that can reach huggingface.co
# (export HTTPS_PROXY=http://127.0.0.1:7890 if needed). The runtime
# transcriber itself never fetches.
python -c "from huggingface_hub import snapshot_download; \
snapshot_download('Systran/faster-whisper-base', \
allow_patterns=['*.bin','*.json','tokenizer.*'])"
```

After this, `ls ~/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/*/`
should contain `model.bin`, `config.json`, `tokenizer.json`.

## 6. Known limitations (do not confuse these with "unavailable")

- **No GPU** — CPU `int8`. Real-time factor ≈ 0.75× on this host for
  `base`. `large-v3-turbo` is cached but slower; switch only when accuracy
  matters more than latency.
- **No diarization** — `whisperx` not in the venv; `HF_TOKEN` unset.
  The tool degrades gracefully: `diarize=True` returns the same segments
  without speaker labels. If a future user needs diarization, install
  `whisperx` + set `HF_TOKEN`; the existing code path
  (`tools/analysis/transcriber.py:357-421`) will pick it up automatically.
- **Language auto-detect adds latency** — pass `language="en"` (or the
  right ISO code) when you know it; otherwise `info.language` is set
  from faster-whisper's first-30-s probe.

## 7. When this doc goes stale

Re-run the §4 script. If the output line still reads
`[OK] <N>s lang=en segments>=1 words>=1 device=cpu artifact=/tmp/...`, this
doc remains correct. If any precondition fails, fix the underlying
condition and update §2 and §4 with the new captured output — don't just
flip the verdict.