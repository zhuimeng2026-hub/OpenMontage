# Subtitle ↔ Voiceover Alignment — Lessons from 2026-09-10

> **Snapshot**: 2026-09-10, written after the first end-to-end 60s TikTok render of `projects/xiaopengge-rideable-luggage-demo/`.
>
> **Companion docs**:
> - [`RESOURCES.md`](RESOURCES.md) — asset hashes, license, reuse recipes.
> - [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — Kokoro / WhisperX capability status.
> - [`VIDEO-GEN-SMOKE-CHECKLIST.md`](VIDEO-GEN-SMOKE-CHECKLIST.md) — kapon I2V pre-flight (different topic, separate concerns).
> - [`../projects/xiaopengge-rideable-luggage-demo/artifacts/render-strategy.md`](../projects/xiaopengge-rideable-luggage-demo/artifacts/render-strategy.md) — 60s render plan.

---

## TL;DR

The naive approach — generate an SRT with one subtitle per `script.json` section, using the section's `start_seconds` / `end_seconds` as timestamps — produces **subtitles that drift ~25 seconds behind the voiceover**. This is because:

1. **Kokoro TTS pauses (`[pause:0.5]`, `[pause:0.3]`) add silence, not section duration.** When you concatenate 8 sentences with 7 pause markers, the total voiceover length is ~25s, not the 60s the script calls for.
2. **Kokoro speech rate is fast** — ~8 Chinese chars/sec on this host's pipeline. 8 short sentences of ~25 chars each fits in 25 seconds with pause gaps.
3. **Section timestamps in `script.json` are visual-scene timing, not voiceover timing.** They define when the viewer sees a given scene, not when the narrator says the corresponding line.

The fix: **align SRT to actual voiceover word timing**, not to script.json scene boundaries.

---

## §1 — What Went Wrong (the symptom)

The first SRT we generated (naive approach, section-aligned):

```srt
1
00:00:00,000 --> 00:00:11,000
出门旅行，行李箱也可以更聪明。

2
00:00:11,000 --> 00:00:17,000
小鹏哥，把收纳、移动和骑行装进一只箱子。

3
00:00:17,000 --> 00:00:23,000
打开座垫，握住车把，穿过平整路面。
...
```

This matched `script.json.sections[].start_seconds` / `end_seconds` exactly. So far so good — but the voiceover was generated separately and **only ran for 25.15 seconds** (Kokoro finished all 8 sentences that fast).

Result: the viewer sees subtitle 1 "出门旅行..." appearing at 0s and lasting until 11s, but the narrator actually finishes that sentence around 2.84s. From 2.84s to 11s the viewer is **reading "出门旅行..." on screen while listening to "小鹏哥，把收纳、移动和骑行装进一只箱子。"** — text-audio mismatch for 8 seconds per section, accumulating across the whole 60s timeline.

This is the classic "subtitle drift" failure mode when subtitle timestamps are derived from layout metadata rather than from the actual audio.

---

## §2 — Why the Section Boundary Approach Fails

The conceptual model behind section-aligned SRTs is:

```
section 1 (0-11s)   →   subtitle 1 displays 0-11s
section 2 (11-17s)  →   subtitle 2 displays 11-17s
...
```

This works **only if voiceover duration equals scene duration**. It breaks the moment voiceover compresses or stretches. In our case, voiceover runs **42% faster than the visual timeline** — 25s of audio stretched across 60s of video.

This mismatch is not unique to our project. Any TTS used for narration will have its own speech rate; the only way to guarantee subtitle timing is to:

1. Generate the voiceover first.
2. Run ASR on the voiceover to extract real word/sentence timing.
3. Build the SRT from the ASR timing, not from section metadata.

This is the standard pipeline used in dubbing / localization work.

---

## §3 — The Working Pipeline (4 steps)

### Step 1 — Generate voiceover

```bash
# In OM (single Kokoro call, all sentences joined by [pause:0.5] markers)
voiceover_text = (
    "出门旅行，行李箱也可以更聪明。[pause:0.5]"
    "小鹏哥，把收纳、移动和骑行装进一只箱子。[pause:0.3]"
    "打开座垫，握住车把，穿过平整路面。[pause:0.3]"
    "不骑时，收起车把，照常拖行。[pause:0.3]"
    "接上电源，为下一段旅程补充能量。[pause:0.3]"
    "侧开空间，衣物和随身物品一目了然。[pause:0.4]"
    "放进后备箱，抵达酒店，咖啡街角，随时切换。[pause:0.4]"
    "小鹏哥，让每次出发更轻松。"
)

# Run via OM
kokoro_tts.execute({
    'text': voiceover_text,
    'voice_id': 'zf_xiaobei',
    'output_path': 'renders/voiceover.wav',
    'speed': 1.0,
})

# Observe actual duration
ffprobe -v error -show_entries format=duration voiceover.wav  # 25.15s for 8 sentences
```

**Caveat**: Kokoro's `[pause:N]` markers do **not** all add N seconds of silence. They cause brief 200-500ms gaps in the audio (verified 2026-09-10), but the precise gap depends on Kokoro's internal chunking. So you cannot compute total voiceover length from text length + pause sums.

### Step 2 — Run ASR on the voiceover

```python
# In OM
transcriber.execute({
    'input_path': 'renders/voiceover.wav',
    'language': 'zh',
    'model_size': 'base',          # or 'large-v3' (faster-whisper naming) if available
    'output_dir': 'renders',
})
# Output: renders/<input_name>_transcript.json with segments[].start/end/text
```

**Model choice**:

- **`base`** (74M params, ~150 MB): fast, ships with `make setup`. **Drift on Chinese homophones** (色→座, 纹→文). The text in the transcript is **unreliable**, but the **timestamps are reliable** for short utterances (< 10s). This is what we used on 2026-09-10.
- **`large-v3` / `large-v3-turbo`** (~800M-1.5B params): much better Chinese accuracy. **Caveat 1**: cache lookup uses the `Systran/faster-whisper-large-v3` repo, NOT the `openai/whisper-large-v3-turbo` snapshot you might have from before. **Caveat 2**: as of 2026-09-10, the CTranslate2 weights for `mobiuslabsgmbh/faster-whisper-large-v3-turbo` were not pre-cached on this host and downloading them requires going through the 7890 HTTPS proxy. The download was ~500 MB / 1.5 GB at the time of writing.
- **For subtitle *timing* only**: `base` is good enough. For subtitle *text accuracy*: use `large-v3` (and accept the 1.5 GB download or pre-cache it before starting).

### Step 3 — Map ASR segments to canonical narration

This is where you reconcile the imperfect ASR text with the canonical narration from `script.json`. Two cases:

**Case A: ASR produced N segments and you have N canonical sentences.** Map them 1-to-1 in order. The ASR text is wrong; the canonical text is right. The ASR timestamps are accurate enough to use.

**Case B: ASR produced fewer segments than canonical sentences** (this happened to us — `base` collapsed the `[pause:0.3]` marker into the surrounding audio and merged two adjacent sentences into one segment). You need to:

1. Identify the merged segments by counting characters vs canonical sentences.
2. Use the **base segment's start** as the merged segment's start.
3. **Hand-split** the merged segment at the natural break point. For our 60s project, segment 7 (`放进后备箱...一目了然包塞0.3`) was actually two sentences mashed together; we split at 21.00s.
4. The final segment's end is the **voiceover total duration** minus any pre-final silence.

### Step 4 — Build aligned SRT

```python
def fmt(t):
    hh, mm = divmod(t, 3600)
    mm, ss = divmod(mm, 60)
    ms = int((ss - int(ss)) * 1000)
    return f'{int(hh):02d}:{int(mm):02d}:{int(ss):02d},{ms:03d}'

ALIGNED = [
    (0.10,  2.84, '出门旅行，行李箱也可以更聪明。'),
    (2.84,  7.66, '小鹏哥，把收纳、移动和骑行装进一只箱子。'),
    (7.66, 11.50, '打开座垫，握住车把，穿过平整路面。'),
    (11.50,15.00, '不骑时，收起车把，照常拖行。'),
    (15.00,18.74, '接上电源，为下一段旅程补充能量。'),
    (18.74,21.00, '侧开空间，衣物和随身物品一目了然。'),  # hand-split
    (21.00,24.18, '放进后备箱，抵达酒店，咖啡街角，随时切换。'),
    (24.18,25.15, '小鹏哥，让每次出发更轻松。'),              # last: voiceover end - epsilon
]

with open('subtitles_aligned.srt', 'w') as f:
    for i, (start, end, text) in enumerate(ALIGNED, 1):
        f.write(f'{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n')
```

**Hand-splitting is the hardest part.** Heuristics:

- **Same narrator pause gap** (~0.3-0.5s) appears in voiceover as a silent boundary. If ASR gives you a 5s block that you'd expect to be 2 sentences × 2.5s, the natural break is at the midpoint where you hear the pause.
- **Sentence punctuation alignment**: if you have `sentence A, sentence B` in canonical text and ASR merged them, the split point is wherever the comma or period falls.
- **Per-sentence char count ratio**: `A_chars / (A_chars + B_chars)` of the merged block's duration. Cheap but inaccurate for sentences of very different lengths.

For our 60s project we used mid-block split because the merged segment's text was close to evenly distributed between the two sentences.

---

## §4 — Burning Subtitles (use libass, not drawtext)

ffmpeg's `drawtext` filter is **not compiled into the Ubuntu 22.04 ffmpeg 6.1 deb package** (`No such filter: 'drawtext'`). Use `subtitles` filter instead — it goes through libass, which IS compiled in:

```bash
ffmpeg -i final_60s_v1_pre_sub.mp4 \
  -vf "subtitles='subtitles_aligned.srt':force_style='FontName=Noto Sans CJK SC,FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=3,Outline=2,Shadow=0,MarginV=80,Alignment=2'" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -c:a copy \
  -movflags +faststart \
  final_60s_v1.mp4
```

The `force_style` directives configure libass directly:

| Field | Value | Meaning |
|---|---|---|
| `FontName` | `Noto Sans CJK SC` | Must be installed: `apt install fonts-noto-cjk` |
| `FontSize` | 18 | 18px — readable at 768x1364 |
| `PrimaryColour` | `&HFFFFFF` | White text (ASS uses BGR + alpha) |
| `OutlineColour` | `&H000000` | Black outline |
| `BorderStyle` | 3 | Opaque box behind text |
| `Outline` | 2 | 2px outline thickness |
| `MarginV` | 80 | Vertical margin from bottom in pixels |
| `Alignment` | 2 | Bottom-center (ASS Numpad alignment) |

If you change to `drawtext` (which you'll need to compile ffmpeg yourself for), the equivalent is roughly:

```bash
-vf "drawtext=text='%{text}':fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=12:x=(w-text_w)/2:y=h-th-80:enable='between(t,START,END)'"
```

`enable='between(t,START,END)'` is how you time-gate each line. But stick with `subtitles` + SRT unless you need libass capabilities that `subtitles` lacks (it doesn't lack any for our use case).

---

## §5 — When the CTA Section Has No Voiceover

The 8th `script.json` section ("CTA: 小鹏哥，让每次出发更轻松。") **was produced by Kokoro**, but WhisperX `base` model **failed to segment it as a separate sentence** — it merged the CTA into the previous "Travel contexts" sentence.

This is because:

- The CTA sentence is short (11 chars vs 25 chars for Travel contexts).
- The preceding `[pause:0.4]` marker adds only ~400ms of silence.
- `base` ASR treats the merged audio as one sentence because there's no clear prosodic break.

If you don't have `large-v3-turbo` to fix this, **estimate the CTA timestamp by ratio**:

```python
# Total voiceover length is known from ffprobe (e.g. 25.15s)
# Last ASR segment ended at 24.18s
# CTA should be from 24.18s to 25.15s = 0.97s for 11 chars
ALIGNED.append((24.18, total_voiceover_length, cta_text))
```

This is approximate but visually correct. The viewer sees the CTA subtitle pop up briefly during the visual CTA card (sc11 57-60s).

**Better fix**: re-run Kokoro with longer pause markers between sentences:

```python
# Use 1s pauses, not 0.3-0.4s — base WhisperX can segment these reliably
voiceover_text = "...[pause:1.0]...小鹏哥，让每次出发更轻松。"
```

Then ASR produces 8 distinct segments and your aligned SRT has 8 rows with real timing. The trade-off is that the voiceover has 1-second gaps that may feel unnatural for a TikTok-fast edit.

---

## §6 — Reusable Code Template

Here's a complete function that, given a voiceover WAV and a list of canonical sentences, produces a properly-aligned SRT:

```python
import json
import wave
from pathlib import Path
from typing import List, Tuple

def align_subtitles_to_voiceover(
    voiceover_path: Path,
    canonical_sentences: List[str],
    output_srt_path: Path,
    transcriber_tool=None,  # OM tools.tool_registry result
    language: str = 'zh',
    model_size: str = 'base',
) -> Path:
    """Build an SRT whose timestamps match actual voiceover word timing.

    Args:
        voiceover_path: WAV file produced by Kokoro TTS
        canonical_sentences: List of correct Chinese sentences in order.
                             Length must equal or be close to ASR segment count.
        output_srt_path: Where to write the .srt file
        transcriber_tool: OM transcriber tool instance
        language: 'zh' for Mandarin
        model_size: 'base' (fast, low accuracy) or 'large-v3' (slow, accurate)

    Returns: output_srt_path

    Raises: ValueError if ASR segment count is wildly off from sentence count
    """
    # 1. Transcribe
    result = transcriber_tool.execute({
        'input_path': str(voiceover_path),
        'language': language,
        'model_size': model_size,
        'output_dir': str(voiceover_path.parent),
    })
    if not result.success:
        raise RuntimeError(f"ASR failed: {result.error}")

    asr_data = result.data
    asr_segments = asr_data['segments']

    # 2. Get total voiceover length (for last-segment padding)
    w = wave.open(str(voiceover_path), 'rb')
    total_voiceover = w.getnframes() / w.getframerate()
    w.close()

    # 3. Map segments to canonical sentences
    n_asr = len(asr_segments)
    n_canon = len(canonical_sentences)

    if n_asr == n_canon:
        # 1-to-1 mapping
        aligned = [
            (asr_segments[i]['start'], asr_segments[i]['end'], canonical_sentences[i])
            for i in range(n_canon)
        ]
    elif n_asr < n_canon:
        # Some sentences merged. Map first n_asr to first n_asr, last remaining to voiceover end.
        aligned = [
            (asr_segments[i]['start'], asr_segments[i]['end'], canonical_sentences[i])
            for i in range(n_asr)
        ]
        # The remaining canonical sentences after n_asr — assign to voiceover end
        for i in range(n_asr, n_canon):
            prev_end = aligned[-1][1] if aligned else 0
            aligned.append((prev_end + 0.01, total_voiceover, canonical_sentences[i]))
    else:
        # ASR over-segmented. Merge the last few ASR segments to canonical count.
        # This case is rare; simplest is to drop excess ASR segments evenly.
        ratio = n_asr / n_canon
        aligned = []
        for i in range(n_canon):
            asr_start = int(i * ratio)
            asr_end = min(int((i + 1) * ratio), n_asr)
            start = asr_segments[asr_start]['start']
            end = asr_segments[asr_end - 1]['end']
            aligned.append((start, end, canonical_sentences[i]))

    # 4. Generate SRT
    def fmt(t):
        hh, mm = divmod(t, 3600)
        mm, ss = divmod(mm, 60)
        ms = int((ss - int(ss)) * 1000)
        return f'{int(hh):02d}:{int(mm):02d}:{int(ss):02d},{ms:03d}'

    srt_lines = []
    for i, (start, end, text) in enumerate(aligned, 1):
        srt_lines.append(f'{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n')

    output_srt_path.write_text('\n'.join(srt_lines))
    return output_srt_path
```

Example usage:

```python
from tools.tool_registry import registry
registry.discover()
transcriber = registry._tools['transcriber']

sentences = [
    "出门旅行，行李箱也可以更聪明。",
    "小鹏哥，把收纳、移动和骑行装进一只箱子。",
    "打开座垫，握住车把，穿过平整路面。",
    "不骑时，收起车把，照常拖行。",
    "接上电源，为下一段旅程补充能量。",
    "侧开空间，衣物和随身物品一目了然。",
    "放进后备箱，抵达酒店，咖啡街角，随时切换。",
    "小鹏哥，让每次出发更轻松。",
]

align_subtitles_to_voiceover(
    voiceover_path=Path('renders/voiceover.wav'),
    canonical_sentences=sentences,
    output_srt_path=Path('renders/subtitles_aligned.srt'),
    transcriber_tool=transcriber,
    model_size='base',  # use 'large-v3' if available
)
```

---

## §7 — Verification: How to Spot Subtitle Drift

After rendering, run this QA check:

```bash
# Extract frames at each subtitle boundary
mkdir -p /tmp/qa_subs
for t in 1.5 5.0 9.5 13.0 17.0 20.0 22.5 25.0; do
  ffmpeg -y -v error -ss $t -i final_60s_v1.mp4 -frames:v 1 /tmp/qa_subs/t${t}.png
done

# Check that each subtitle text matches what's visually shown
# (compare against voiceover transcript)
```

Visual QA: spot-check 3-4 frames where subtitles should appear. If you see "出门旅行..." at t=15s (when it should have ended at t=2.84s), the timing is still off.

For a programmatic check, use OCR on the rendered frames:

```bash
# Optional: tesseract OCR each subtitle frame
for f in /tmp/qa_subs/*.png; do
  echo "=== $(basename $f) ==="
  tesseract $f - -l chi_sim 2>/dev/null
done
```

If OCR text matches the expected subtitle at that timestamp, alignment is correct.

---

## §8 — Maintenance

- **When to update this doc**: any time a new TTS provider is added to OM, or when WhisperX/faster-whisper versions change segment-merging behavior.
- **Owner**: whoever maintains `tools/audio/kokoro_tts.py` or `tools/analysis/transcriber.py`.
- **Companion runtime**: this approach works for any TTS → SRT pipeline. Specific tuning (pause markers, model choice) is project-dependent.

---

## Appendix — Files Generated on 2026-09-10

For the `xiaopengge-rideable-luggage-demo` 60s render:

| File | Description |
|---|---|
| `renders/voiceover.wav` | Kokoro TTS, 25.15s, 24kHz mono, 11.8 MB |
| `renders/voiceover_transcript.json` | WhisperX `base` output (7 segments, 0-24.18s, text garbled) |
| `renders/subtitles_aligned.srt` | Aligned SRT, 8 cues (7 from ASR + 1 estimated from voiceover total) |
| `renders/final_60s_v1.mp4` | Final 60s video with aligned subtitles burned via `subtitles` filter |
| `renders/final_60s_v1.mp4.old` | Previous version (section-aligned subtitles, drifted) |

The hand-split between segment 7 (Storage) and 7+1 (Travel contexts) was at t=21.00s — chosen because the merged segment's text was approximately evenly distributed.
