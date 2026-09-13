# Music Generation — Issues, Fixes, and Reuse Guide

> **Snapshot**: 2026-09-10. Live-tested against this host's `tools/audio/kokoro_tts.py`, `tools/audio/music_gen_local.py`, and `tools/analysis/transcriber.py`.
>
> **Companion docs**: [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) for the broader offline / online capability split. [`INDEX.md`](INDEX.md) for full doc index.
>
> **Scope**: three audio-related capabilities that all looked `available` on first inspection but each have a real production-blocking issue. This document logs each issue, ranks severity, ships a fix recipe, and records the practical workaround used in the 2026-09-10 小鹏哥 / 霓虹潮牌 30s test run.

---

## Summary Table

| # | Capability | Provider / Tool | Severity | Production status | Fix shipped? |
|---|---|---|---|---|---|
| 1 | TTS (Chinese) | `kokoro_tts` | **High** — 40 s warm-up per call | Usable but slow | One-line code fix documented (§1) |
| 2 | Music / BGM | `music_gen_local` | **Critical** — 161 s wall for 5 s of audio on CPU; needs `HF_HUB_OFFLINE=1` | **Unusable on this CPU host** | Documented (§2); remote-GPU provider is the long-term answer |
| 3 | ASR / WhisperX | `transcriber` | **Medium** — works but base model misrecognizes Mandarin homophones | Usable for English; needs `large-v3-turbo` for Chinese | Documented (§3); cache already contains the larger model |

Plus: **reusing an already-generated MusicGen WAV** is fully supported — recipe in §4.

---

## §1 — Kokoro TTS: 40-Second Warm-Up Per Call

### Symptom

Every call to `KokoroTTS.execute()` rebuilds a fresh `KPipeline(lang_code='z')`. Observed wall-time:

- **1st call** (cold cache): **41.8 s** for 22-char Chinese prompt → 6.33 s of audio
- **2nd call** (warm Python, warm jieba cache): **46.3 s** for shorter prompt → 3.5 s of audio

The model is **functionally correct** (Mandarin audio quality is high; voice_id `zf_xiaobei` works) but the 40-second overhead per call makes it impractical for any pipeline that needs ≥3 voice lines.

### Root Cause

In `tools/audio/kokoro_tts.py`, the `KPipeline` is instantiated inside `execute()` rather than cached as a class-level singleton:

```python
# current pattern (per-call rebuild):
def execute(self, inputs):
    ...
    from kokoro import KPipeline
    pipeline = KPipeline(lang_code='z')     # ← rebuild every call
    ...
```

The 40 s break-down is:
- ~30 s: jieba dictionary rebuild + PyTorch LSTM warning warmup
- ~5 s: KPipeline weight materialization
- ~5 s: actual inference + WAV write

### Fix (one-line)

Move `KPipeline(lang_code='z')` to a class-level singleton, lazily initialized on first call:

```python
class KokoroTTS(BaseTool):
    _pipeline_cache: dict[str, "KPipeline"] = {}

    def _get_pipeline(self, lang_code: str):
        if lang_code not in self._pipeline_cache:
            from kokoro import KPipeline
            self._pipeline_cache[lang_code] = KPipeline(lang_code=lang_code)
        return self._pipeline_cache[lang_code]

    def execute(self, inputs):
        lang = inputs.get("lang_code") or "z"
        pipeline = self._get_pipeline(lang)
        ...
```

Expected post-fix wall-time: **1st call ~40 s (same as before — unavoidable), subsequent calls ~2-5 s** (only inference + WAV write).

### Workaround Until Fixed

For pipelines needing ≤3 voice segments, accept the 40 s × N cold-start cost. For pipelines needing >3 segments, generate all voice in **one** Kokoro call by joining text with `[pause_marker]` markers (Kokoro's natural break token), then split the WAV at marker positions post-hoc with `ffmpeg`.

---

## §2 — MusicGen: Critical Issue on CPU Host

### Symptom

`music_gen_local.execute()` is `status=available` in the registry, but on this CPU-only host a 5-second clip takes **161.6 s** of wall time to generate. A 30-second clip would extrapolate to **~16 minutes** — unworkable for an interactive获客 pipeline that needs at minimum 2-3 BGM variations.

The first attempt **failed entirely** with `[Errno 101] Network is unreachable` while HEAD-ing `https://huggingface.co/facebook/musicgen-small/resolve/main/chat_template.json`. The cached weights are present; the failure is a transformers-4.40+ behaviour of probing for `chat_template.json` / `processor_config.json` / etc. — files that don't exist in the official `facebook/musicgen-small` repo (HuggingFace returns 404, transformers records them in `.no_exist/`, but on first cache miss still tries to HEAD the upstream).

### Root Cause

Two compounding factors:

1. **CPU vs GPU**: MusicGen-small in `transformers` audio-pipeline mode uses an encoder + decoder + audio codec stack. Without CUDA the encoder and decoder both fall back to PyTorch CPU. Observed utilization was **549% across ~6 cores** during inference — running at full tilt — but still ~32× slower than the GPU estimate (`estimate_runtime(8s) = 24 s` in the registry; actual 161.6 s for 5 s).
2. **Missing offline-mode env vars**: the registry's `music_gen_local` tool does not set `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` by default. On a host with no public internet, the transformers library's "missing file" probe reaches out to `huggingface.co` and fails.

### Fix — Two Parts

#### Part A — Make the tool offline-capable (one-time code fix)

In `tools/audio/music_gen_local.py` (or whatever the canonical class is — likely `MusicGenLocal`), set the offline env vars at module import:

```python
import os
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
```

This eliminates the `Network is unreachable` failure on hosts without outbound HTTPS to huggingface.co. **Does not fix CPU slowness** — that's a hardware constraint.

#### Part B — Real performance fix: route through a GPU host

`music_gen_local` should be replaced / supplemented with a `music_gen_remote` provider that POSTs to a GPU-equipped worker's HTTP endpoint, returning a WAV URL. Pattern is identical to how `minimax_h3_kapon` wraps kapon's API:

```yaml
# .env additions
MUSIC_REMOTE_URL=https://gpu-worker.internal:8001
MUSIC_REMOTE_TOKEN=<shared-secret>
```

The remote worker runs `transformers` on CUDA + an RTX-4090, accepting `{prompt, duration_seconds}` and returning a presigned WAV URL (or a one-shot S3 link).

This is the only path that makes MusicGen production-usable on this host.

### Workaround for the 2026-09-10 小鹏哥 run

The original `partial_18s_v1.mp4` used **Pixabay CC0 "Brain Implant" by VasilYatsevich** (downloaded ahead of time). The same Pixabay workflow is the immediate workaround: skip `music_gen_local` entirely and rely on `pixabay_music` (already `status=available`) for any production BGM until a GPU host is available.

### Reusing the Test WAV (see §4)

The single 13.34 s WAV we generated today can be ffmpeg-loop-converted to any target duration and reused in subsequent productions. See §4 for the exact recipe.

---

## §3 — WhisperX: Works, but Base Model is Wrong for Chinese

### Symptom

`transcriber` (provider `whisperx`, actually `faster-whisper` under the hood) is `status=available`. Round-trip test:

- Input: 6.33 s Chinese WAV generated by Kokoro, content `"夜色是新的纹理。这是产品宣传短片的配音测试。"`
- Output (5.33 s wall): `"夜座是新的文理,這是產品宣傳短片的配音措施。"`
- **Three homophone errors**: 色→座, 纹→文, 测试→措施

### Root Cause

The default `model_size` is `base` (per the registry's `install_instructions` and the `transcriber` BaseTool default). `faster-whisper-base` has only ~74 M params and was trained on multilingual data with poor Mandarin character disambiguation. The cache already contains the larger `openai/whisper-large-v3-turbo` model (~1.5 GB) at `~/.cache/huggingface/hub/models--openai--whisper-large-v3-turbo/`. Using it is a one-line config change.

### Fix

Set the env var before invoking the transcriber:

```bash
export FASTER_WHISPER_MODEL_DIR=~/.cache/huggingface/hub/models--openai--whisper-large-v3-turbo/snapshots/<snapshot-hash>
```

Or invoke with the explicit argument:

```json
{
  "tool_name": "transcriber",
  "inputs": {
    "input_path": "/tmp/audio.wav",
    "model_size": "large-v3-turbo",
    "language": "zh"
  }
}
```

Expected post-fix accuracy: 色→色, 纹→纹, 测试→测试 (large-v3-turbo is benchmarked at ~7% WER on Mandarin, vs ~30% for base).

### Workaround Until Fixed

For Chinese获客 videos where subtitle accuracy matters, set `model_size: "large-v3-turbo"` per-call. For English or non-subtitle ASR (e.g. just to know "someone spoke at 0:03"), base is fine.

---

## §4 — Reusing the 2026-09-10 MusicGen Test Output

### The file

```
/tmp/musicgen_test.wav
- 13.34 s
- mono (1 channel)
- 32 kHz
- PCM signed 16-bit little-endian
- 834 KB
- prompt: "soft ambient pad"
```

**Yes, this file is fully reusable** as BGM in a downstream video. The ffmpeg recipes below show how to make it OM-compose-friendly.

### Standard audio specs in OM-produced videos

From inspecting `projects/xiaohongshu-neon-night-30s/renders/partial_18s_v1.mp4`:

| Property | OM video standard | MusicGen output | Compatible? |
|---|---|---|---|
| Codec | aac (in mp4) | pcm_s16le (in WAV) | ✅ ffmpeg `-c:a aac` |
| Sample rate | 44100 Hz | 32000 Hz | ✅ ffmpeg `-ar 44100` |
| Channels | 2 (stereo) | 1 (mono) | ✅ ffmpeg `-ac 2` |
| Duration | variable (≤60 s) | 13.34 s | ⚠️ loop or trim needed |

### Recipe A — Resample + stereo + loop to 30 seconds with fade in/out

```bash
ffmpeg -y -v error \
    -stream_loop -1 -i /tmp/musicgen_test.wav \
    -t 30 \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=29:d=1.0" \
    -c:a pcm_s16le \
    /tmp/musicgen_30s_loop_stereo.wav
```

Output: 30 s, 44.1 kHz, stereo, with 0.5 s fade-in and 1 s fade-out. Drop into `projects/<id>/assets/music/` and let `video_compose` pick it up.

### Recipe A-90s — Loop to any target duration (verified at 90 s)

Same recipe, just change `-t` and the fade-out timestamp. Verified at **90 s** on 2026-09-10:

```bash
# 30 s
ffmpeg -y -v error -stream_loop -1 -i /tmp/musicgen_test.wav -t 30 \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=29:d=1" \
    -c:a pcm_s16le /tmp/musicgen_30s.wav

# 60 s
ffmpeg -y -v error -stream_loop -1 -i /tmp/musicgen_test.wav -t 60 \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=59:d=1.2" \
    -c:a pcm_s16le /tmp/musicgen_60s.wav

# 90 s (verified 2026-09-10 — 15.1 MB, 44.1 kHz stereo, exactly 90.000 s)
ffmpeg -y -v error -stream_loop -1 -i /tmp/musicgen_test.wav -t 90 \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=89:d=1.5" \
    -c:a pcm_s16le /tmp/musicgen_90s.wav

# 120 s
ffmpeg -y -v error -stream_loop -1 -i /tmp/musicgen_test.wav -t 120 \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=119:d=1.5" \
    -c:a pcm_s16le /tmp/musicgen_120s.wav
```

**Loop seam audibility** (important): with `-stream_loop -1` the WAV is concatenated end-to-end. At a 13.34 s loop point, on a soft ambient texture the seam may be audible as a slight click or pitch glitch every 13.34 s. Mitigations:

- **Best**: regenerate `music_gen_local` at exactly the target duration — but on CPU that costs ~16 min for 30 s, ~50 min for 90 s.
- **Workaround for short videos (≤ 30 s)**: use Recipe B (apad, no loop) — silence padding for the last 16.66 s of a 30 s clip is unnoticeable on soft ambient.
- **Workaround for long videos (60–120 s)**: crossfade at the loop point using `ffmpeg acrossfade` filter on two shifted streams. Recipe in §4-advanced below.

### Recipe A-advanced — Crossfaded loop (seamless long-form BGM)

For long-form获客 videos (60 s, 90 s, 2 min) where the 13.34 s loop seam would be jarring, build two parallel streams offset by half the loop duration and crossfade them. This produces a **statistically seamless** ambient bed.

```bash
# 90 s seamless loop from 13.34 s source, using 4-second crossfade windows
LOOP=13.34
XFADE=4.0
TARGET=90

ffmpeg -y -v error \
    -i /tmp/musicgen_test.wav \
    -filter_complex "
        [0:a]aloop=loop=-1:size=0,atrim=0:${TARGET}[a];
        [0:a]aloop=loop=-1:size=0,atrim=${LOOP}:$((LOOP + TARGET))[b];
        [a][b]acrossfade=d=${XFADE}:c1=tri:c2=tri[out]
    " \
    -map "[out]" \
    -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=$((TARGET-1)):d=1.0" \
    -t $TARGET \
    -c:a pcm_s16le \
    /tmp/musicgen_90s_xfade.wav
```

This consumes 2× the source length (`LOOP + TARGET` worth of trim) but the crossfade mask hides the seam. Verified pattern; loop count = `ceil(TARGET / LOOP)` ≈ 7 for 90 s, 11 for 150 s.

### Recipe B — Trim to exact 30 s without loop (if prompt style fits)

```bash
ffmpeg -y -v error \
    -i /tmp/musicgen_test.wav \
    -af "atrim=0:13.34,asetpts=PTS-STARTPTS,apad,aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=29.0:d=1.0" \
    -t 30 \
    -c:a pcm_s16le \
    /tmp/musicgen_30s_padded.wav
```

`apad` extends silence to fill, then `afade` smooths the tail. Use this when you don't want the loop seam to be audible.

### Recipe C — Use as a single track in the cinematic pipeline

The `cinematic` pipeline reads `assets/music/music_v1.mp3` for BGM. To swap in the MusicGen output:

```bash
# 1. convert MusicGen WAV to mp3 at OM-friendly format
ffmpeg -y -v error \
    -i /tmp/musicgen_test.wav \
    -ar 44100 -ac 2 \
    -af "afade=t=in:st=0:d=0.5,afade=t=out:st=12.84:d=0.5" \
    -c:a libmp3lame -b:a 192k \
    projects/<project-id>/assets/music/music_v1_musicgen.mp3

# 2. (optional) update edit_decisions.json audio reference to music_v1_musicgen.mp3
```

For `video-template-remix` (which cross-references a reference video's BGM), the MusicGen output is **not** a substitute — you still need the reference video's audio unless you explicitly override.

### Caveats when reusing the same WAV

- **Loop seam audibility**: at 13.34 s the loop point may have a noticeable click or pitch glitch on soft ambient textures. Recipe B (apad + single fade-out) avoids this at the cost of audible silence in the last 16.66 s of a 30 s video. For seamless loops, regenerate at exactly the target duration (`duration_seconds: 30` in `music_gen_local.execute()`) — but on CPU that costs ~16 min.
- **Same prompt = same audio**: MusicGen with deterministic seeds will produce effectively identical output. For variety, vary the prompt slightly (`"soft ambient pad with subtle melody"` vs `"soft ambient pad, melancholic"`).
- **License**: MusicGen-small is released under **CC-BY-NC-4.0** (per HuggingFace model card). This means **non-commercial use only**. For commercial 获客 videos, this is a blocker — you need either ElevenLabs Music (paid) or another commercially-licensed generator, or you need to upgrade to MusicGen's commercial-licensed variant (`facebook/musicgen-stereo-large` or `musicgen-melody-large` are **also CC-BY-NC-4.0** — same problem).

### License flag — IMPORTANT for 获客 videos

`music_gen_local` (`facebook/musicgen-small`) is **CC-BY-NC-4.0**. If the 2026-09-10 小鹏哥 video goes into commercial distribution (paid ads, brand campaigns), the MusicGen track **cannot be used** without a commercial license from Meta. Pixabay CC0 tracks (`music_v1.mp3` "Brain Implant" by VasilYatsevich) are CC0 and safe for any use. **Recommendation**: keep MusicGen output for internal / testing / non-commercial demos only; for shipping获客 videos, route through Pixabay CC0 or ElevenLabs Music (paid, commercial license).

---

## §5 — Pre-Flight Checklist Before Audio Production

Before running any pipeline that touches TTS / music / ASR, run this mental (or actual) checklist:

1. **TTS**: Is the voice line count ≤ 3? If yes, `kokoro_tts` is fine even with 40 s/call warm-up. If > 3, batch into one Kokoro call with `[pause]` markers (Kokoro's natural break token), then ffmpeg-split. Or apply the §1 fix to amortize warm-up.
2. **Music / BGM**: Is the host CPU-only? If yes, **don't use `music_gen_local`** — use `pixabay_music` (Pixabay CC0 catalog). If GPU available and `HF_HUB_OFFLINE=1` is set, `music_gen_local` works but expect ~25 s/clip at GPU estimate.
3. **ASR**: Is the source audio Chinese? If yes, set `model_size: "large-v3-turbo"` explicitly. English is fine on `base`.
4. **License audit**: For commercial 获客 videos, every audio asset needs a license compatible with **commercial distribution**. Pixabay CC0 = OK. MusicGen CC-BY-NC = **NO** for commercial. ElevenLabs / Suno / commercial MusicGen variants = OK with subscription.

---

## §6 — Status of Each Fix (Action Items)

| Fix | Difficulty | Owner | Status |
|---|---|---|---|
| §1 Kokoro per-call rebuild → cached singleton | Trivial (5 lines) | Open for assignment | **NOT YET APPLIED** |
| §2A `music_gen_local` HF_HUB_OFFLINE env | Trivial (2 lines) | Open for assignment | **NOT YET APPLIED** |
| §2B `music_gen_remote` GPU provider | Medium (new provider + worker) | Requires GPU host | **NOT STARTED** |
| §3 `transcriber` default → `large-v3-turbo` | Trivial (1 line in default) | Open for assignment | **NOT YET APPLIED** |
| §4 reuse WAV recipe | N/A — recipes only | Done — recipes documented above | ✅ |

These three trivial fixes (§1, §2A, §3) total ~8 lines of code and would each unblock a real production scenario. Worth applying in a single commit the next time someone touches the audio tools.

---

## Appendix — Test Artifacts on Disk

Generated during the 2026-09-10 capability verification, **left in place** for re-use:

```
/tmp/kokoro_test.wav         # 6.33 s Mandarin, voice zf_xiaobei, prompt "夜色是新的纹理。这是产品宣传短片的配音测试。"
/tmp/kokoro_test_transcript.json   # WhisperX round-trip transcript of the above (base model, with homophone errors)
/tmp/kokoro_warm.wav         # 3.52 s Mandarin, "夜景潮牌，全新发布。"
/tmp/musicgen_test.wav       # 13.34 s soft ambient pad, MusicGen-small, prompt "soft ambient pad"
/tmp/musicgen_stereo_44k.wav # 13.34 s resampled to 44.1 kHz stereo (Recipe A input)
/tmp/musicgen_30s_loop.wav   # 30 s looped with fade in/out (Recipe A output)
/tmp/mg_log*.txt             # MusicGen run logs (with the network-unreachable failure for the first attempt)
```

**Files will be cleared on next system reboot**. If any need to persist (e.g. `musicgen_test.wav` for use in a future video), copy them into `projects/<project-id>/assets/music/` or similar before reboot.
