# Subtitle Alignment Tooling — `utils/`

> **Snapshot**: 2026-09-10. Companion to [`SUBTITLE-VOICEOVER-ALIGNMENT.md`](SUBTITLE-VOICEOVER-ALIGNMENT.md) which explains the *why*; this doc explains the *how*.
>
> **Audience**: anyone producing Kokoro voiceovers who needs subtitle timing that matches real audio, not script.json section boundaries.

---

## What this tooling does

Three scripts in `utils/` implement the alignment pipeline from `SUBTITLE-VOICEOVER-ALIGNMENT.md` as runnable code:

| Script | Purpose |
|---|---|
| `calibrate_subtitle_alignment.py` | Capture host-specific environment fingerprint (Kokoro speed, WhisperX behavior) → `calibration.json` |
| `align_subtitles.py` | One-shot aligner: ASR voiceover → map to canonical sentences → write aligned SRT |
| `run_alignment_pipeline.py` | E2E wrapper around `align_subtitles.py` + `calibrate_subtitle_alignment.py` |

The key insight: **alignment strategy depends on environment** (Kokoro version, WhisperX model, ASR cache state). These scripts capture that as a calibration fingerprint so the same code produces correct SRTs across hosts.

---

## Quick start

```bash
cd /opt/OpenMontage_Voicebox

# 1. One-time per host / environment change
.venv/bin/python utils/calibrate_subtitle_alignment.py \
    --output artifacts/calibration.json

# 2. Per-video alignment
.venv/bin/python utils/run_alignment_pipeline.py \
    --voiceover projects/<project>/renders/voiceover.wav \
    --sentences-file projects/<project>/renders/sentences.json \
    --calibration artifacts/calibration.json \
    --output projects/<project>/renders/subtitles_aligned.srt

# 3. Burn into video
ffmpeg -i projects/<project>/renders/final_pre_sub.mp4 \
  -vf "subtitles='projects/<project>/renders/subtitles_aligned.srt':force_style='FontName=Noto Sans CJK SC,FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=3,Outline=2,Shadow=0,MarginV=80,Alignment=2'" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -c:a copy -movflags +faststart \
  projects/<project>/renders/final.mp4
```

---

## `calibrate_subtitle_alignment.py`

### What it does

Synthesizes a fixed 8-sentence probe through Kokoro, runs WhisperX on the result, and writes a `calibration.json` summarizing:

- **Kokoro characteristics**: speech rate (chars/sec), warning if too fast/slow
- **WhisperX characteristics**: segment count vs canonical, which segments were merged
- **Implications**: human-readable hints for downstream alignment code

### Why a probe instead of reading the live project audio?

Two reasons:

1. **Predictability**: The 8-sentence probe has known canonical text. This lets the calibration tool detect when ASR collapses multiple sentences into one (a common failure mode with `base` model + short utterances).
2. **Independence**: Calibration can run before the actual project audio exists. It tests the *tools*, not the *content*.

### Usage

```bash
.venv/bin/python utils/calibrate_subtitle_alignment.py \
    --tts-tool kokoro_tts \
    --asr-tool transcriber \
    --voice-id zf_xiaobei \
    --language zh \
    --model-size base \
    --probe-audio /tmp/kokoro_probe.wav \
    --output artifacts/calibration.json
```

All defaults are tuned for the most common OM setup. Override only if you've intentionally changed TTS voice or ASR model.

### Output: `calibration.json`

```json
{
  "captured_at": "2026-09-10T22:59:28+08:00",
  "host": {"hostname": "xt", "python_version": "3.10.12"},
  "kokoro": {
    "success": true,
    "voice_id": "zf_xiaobei",
    "lang_code": "z",
    "8_sentences_with_pause_03_total_duration_sec": 25.25,
    "char_count": 133,
    "chars_per_second": 5.27,
    "wall_seconds": 64.7,
    "warning": null
  },
  "whisperx": {
    "success": true,
    "model_size": "base",
    "language": "zh",
    "segment_count": 6,
    "canonical_count": 8,
    "merged_segments": [],
    "truncation_warning": null,
    "warning": "ASR produced 6 segments for 8 canonical sentences..."
  },
  "implications": [
    "ASR produced 6/8 segments; 2 segment(s) merged or missing; CTA section must be estimated from voiceover total length."
  ]
}
```

### Reading the calibration output

| Field | Healthy value | Action if not |
|---|---|---|
| `kokoro.8_sentences_with_pause_03_total_duration_sec` | 20-30s | If >60s: cold load or wrong voice. If <10s: too fast, check voice_id |
| `kokoro.chars_per_second` | 4-10 chars/sec | Same as above |
| `whisperx.segment_count` | ≥ 7 (out of 8 canonical) | If 6 or fewer: hand-split needed |
| `whisperx.merged_segments` | empty or [6] | ASR model is collapsing sentences — consider switching to `large-v3` |
| `implications` | 1-2 short hints | Empty list = calibration clean, no special handling |

### When to re-calibrate

Re-run when:
- Kokoro version changes (`pip install --upgrade kokoro`)
- WhisperX model changes (switching `base` → `large-v3`)
- `lang_code` changes (`z` → `a` for English)
- `voice_id` changes
- Host changes (new machine, new VM)
- ASR cache invalidates

---

## `align_subtitles.py`

### What it does

Given a voiceover WAV and a list of canonical sentences, produces an SRT whose timestamps match real voiceover timing.

The strategy is chosen dynamically based on ASR output vs canonical count:

| ASR segments vs canonical | Strategy |
|---|---|
| Equal count | 1-to-1 mapping (no split) |
| ASR < canonical | Hand-split merged segments using char-count ratio; CTA-style end sentences estimated from voiceover total length |
| ASR > canonical (rare) | Merge excess ASR segments evenly across canonical sentences |

### Usage

```bash
.venv/bin/python utils/align_subtitles.py \
    --voiceover renders/voiceover.wav \
    --sentences-file sentences.json \
    --calibration artifacts/calibration.json \
    --output renders/subtitles_aligned.srt
```

### Input formats

The script accepts canonical sentences in three ways:

**1. Inline pipe-separated**:

```bash
.venv/bin/python utils/align_subtitles.py \
    --voiceover v.wav \
    --sentences-text "出门旅行...小聪明。|小鹏哥...一只箱子。|..." \
    --output out.srt
```

**2. JSON file (list)**:

```json
[
  "出门旅行，行李箱也可以更聪明。",
  "小鹏哥，把收纳、移动和骑行装进一只箱子。",
  "..."
]
```

```bash
.venv/bin/python utils/align_subtitles.py \
    --voiceover v.wav \
    --sentences-file sentences.json \
    --output out.srt
```

**3. JSON file (object with "sentences" key)**:

```json
{
  "sentences": ["...", "...", "..."]
}
```

### Programmatic use

```python
from pathlib import Path
from utils.align_subtitles import align_subtitles_to_voiceover

align_subtitles_to_voiceover(
    voiceover_path=Path('renders/voiceover.wav'),
    canonical_sentences=['sentence 1', 'sentence 2', '...'],
    output_srt_path=Path('renders/subtitles_aligned.srt'),
    asr_tool=registry._tools['transcriber'],  # OM tool instance
    calibration_path=Path('artifacts/calibration.json'),
)
```

---

## `run_alignment_pipeline.py`

### What it does

Convenience wrapper. If calibration is missing, runs it first; then runs align. Useful when you don't want to remember the two-step order.

### Usage

```bash
.venv/bin/python utils/run_alignment_pipeline.py \
    --voiceover renders/voiceover.wav \
    --sentences-file sentences.json \
    --output renders/subtitles_aligned.srt \
    --auto-calibrate
```

With `--auto-calibrate`, calibration runs first and writes to `renders/calibration.json` (next to the voiceover). Without it, the script looks for `artifacts/calibration.json` first, then `../artifacts/calibration.json`, then `voiceover-parent/calibration.json`.

---

## Choosing the right invocation

| Scenario | Use |
|---|---|
| First time on this host, no calibration exists | `calibrate_subtitle_alignment.py` first, then `run_alignment_pipeline.py` |
| Same host, new project, calibration still valid | `run_alignment_pipeline.py` (skip calibration) |
| Pipeline in CI / batch processing | `run_alignment_pipeline.py --auto-calibrate` (always re-calibrate, takes ~70s) |
| Just one video, no CI | `align_subtitles.py` directly, with `--calibration` |
| Programmatic use from another tool | `align_subtitles_to_voiceover()` function |

---

## Reading the output SRT

After running, inspect the output:

```bash
cat renders/subtitles_aligned.srt
```

Expected format:

```
1
00:00:00,080 --> 00:00:02,839
出门旅行，行李箱也可以更聪明。

2
00:00:02,839 --> 00:00:07,660
小鹏哥，把收纳、移动和骑行装进一只箱子。
...
```

### Spot-check: are subtitles drifting from voiceover?

```bash
# Extract frames at each subtitle boundary
mkdir -p /tmp/sub_qa
for t in 1.5 5.0 9.5 13.0 17.0 20.0 22.5 24.5; do
  ffmpeg -y -v error -ss $t -i renders/final.mp4 -frames:v 1 /tmp/sub_qa/t${t}.png
done

# Visual check: does the text on screen match what's being said at that time?
```

If subtitle 2 ("小鹏哥...") is visible at t=1.5s (when narrator is still saying "出门旅行..."), alignment is broken. Re-run `calibrate_subtitle_alignment.py` and check implications.

### Compare timing against voiceover

```python
# Quick verification: does SRT cue count match voiceover sentences?
from pathlib import Path
import wave

voiceover_dur = wave.open('renders/voiceover.wav','rb').getnframes() / wave.open('renders/voiceover.wav','rb').getframerate()
srt_lines = Path('renders/subtitles_aligned.srt').read_text().strip().split('\n\n')
last_cue_end = srt_lines[-1].split('\n')[1].split(' --> ')[1]

print(f'voiceover duration: {voiceover_dur:.2f}s')
print(f'last cue ends at: {last_cue_end}')
```

If last cue ends significantly before voiceover duration, you have missing cues (likely CTA sentences that ASR dropped).

---

## Common failure modes

### Mode 1: Kokoro returns `success: false`

```json
{
  "kokoro": {
    "success": false,
    "error": "KAPON_API_TOKEN not set. ...",
    ...
  }
}
```

**Cause**: missing env var or wrong voice_id.

**Fix**:
- Check `grep KAPON_API_TOKEN /opt/OpenMontage_Voicebox/.env`
- Check `voice_id` exists in Kokoro (run `kokoro_tts.get_info()` programmatically)

### Mode 2: WhisperX returns `success: false`

```json
{
  "whisperx": {
    "success": false,
    "error": "faster-whisper model 'large-v3' not found in HF cache ...",
    ...
  }
}
```

**Cause**: trying to use a WhisperX model variant whose CTranslate2 weights aren't cached.

**Fix**:
- For `base`: it's pre-cached in `~/.cache/huggingface/hub/models--Systran--faster-whisper-base/`. Should work out of the box.
- For `large-v3` / `large-v3-turbo`: download via `mobiuslabsgmbh/faster-whisper-large-v3-turbo` (≈1.5 GB). Requires HTTPS_PROXY if behind a firewall.

### Mode 3: ASR produces too few segments

```json
{
  "whisperx": {
    "segment_count": 5,
    "canonical_count": 8,
    ...
  },
  "implications": [
    "ASR produced 5/8 segments; 3 segment(s) merged or missing; CTA section must be estimated from voiceover total length."
  ]
}
```

**Cause**: short utterances or pause markers confuse the ASR.

**Fix**:
- Try `large-v3` instead of `base` (better Chinese handling).
- Use longer pause markers in Kokoro narration: `[pause:0.5]` or `[pause:1.0]`.
- Accept the alignment produced by `align_subtitles.py` (it handles this case via CTA estimation).

### Mode 4: ASR produces too many segments

```json
{
  "whisperx": {"segment_count": 15, "canonical_count": 8}
}
```

**Cause**: ASR is splitting on tiny pauses that don't correspond to sentence boundaries.

**Fix**: `align_subtitles.py` handles this automatically by merging excess ASR segments evenly across canonical sentences. No action needed.

---

## Programmatic use example

Combining calibration + alignment + video burn in one Python script:

```python
#!/usr/bin/env python3
"""Run the full subtitle pipeline and burn into video."""
import sys
from pathlib import Path
sys.path.insert(0, '/opt/OpenMontage_Voicebox')
from tools.tool_registry import registry
from utils.calibrate_subtitle_alignment import calibrate
from utils.align_subtitles import align_subtitles_to_voiceover

registry.discover()

RENDERS = Path('/opt/OpenMontage_Voicebox/projects/myproj/renders')

# 1. Calibrate
cal = calibrate(
    tts_tool=registry._tools['kokoro_tts'],
    asr_tool=registry._tools['transcriber'],
    output_path=RENDERS / 'calibration.json',
)

# 2. Run Kokoro voiceover (your existing code)
# 3. Align
sentences = ['...', '...']  # from your script.json
align_subtitles_to_voiceover(
    voiceover_path=RENDERS / 'voiceover.wav',
    canonical_sentences=sentences,
    output_srt_path=RENDERS / 'subtitles_aligned.srt',
    asr_tool=registry._tools['transcriber'],
    calibration_path=RENDERS / 'calibration.json',
)

# 4. Burn into video via ffmpeg
import subprocess
subprocess.run([
    'ffmpeg', '-y', '-v', 'error',
    '-i', str(RENDERS / 'final_pre_sub.mp4'),
    '-vf', f"subtitles='{RENDERS / 'subtitles_aligned.srt'}':force_style='FontName=Noto Sans CJK SC,FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=3,Outline=2,Shadow=0,MarginV=80,Alignment=2'",
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
    '-c:a', 'copy', '-movflags', '+faststart',
    str(RENDERS / 'final.mp4'),
], check=True)
```

---

## Maintenance

- **When to update `calibrate_subtitle_alignment.py`**: when Kokoro's API changes (model install, voice list, pause marker syntax), or when WhisperX/faster-whisper changes model names.
- **When to update `align_subtitles.py`**: when you need new strategies (e.g. diarization-aware alignment, language detection).
- **When to update this doc**: when adding a third script or changing the recommended workflow.

---

## See also

- [`SUBTITLE-VOICEOVER-ALIGNMENT.md`](SUBTITLE-VOICEOVER-ALIGNMENT.md) — full explanation of why alignment is non-trivial
- [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — Kokoro and WhisperX capability status
- [`RESOURCES.md`](RESOURCES.md) — assets and license
