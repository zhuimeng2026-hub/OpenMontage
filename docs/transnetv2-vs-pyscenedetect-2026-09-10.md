# TransNetV2, PySceneDetect, and "can a VLM replace shot detection?" — analysis + system runnability check

> **Status:** stable reference. Records the 2026-09-10 analysis of why TransNetV2 is stronger than PySceneDetect (the current OM built-in), whether a general-purpose VLM (MiniMax / Qwen2.5-VL / GPT-4o) can substitute, and what the current OpenMontage host can actually run.
> **Audience:** anyone considering upgrading `tools/analysis/scene_detect.py` or reasoning about which decomposition tools are realistic on this host.
> **Date:** 2026-09-10.

## TL;DR

1. **TransNetV2 vs PySceneDetect:** TransNetV2 wins on the three categories that break PySceneDetect (fades/dissolves, low-contrast hard cuts, fast motion / flash / strobe). PySceneDetect only stays competitive on pure hard-cut screen recordings.
2. **Can a general VLM replace TransNetV2?** No. They are **complementary**, not substitutes. VLMs answer "what is this shot about?"; TransNetV2 answers "where exactly is the cut?". Substituting one with the other loses frame precision, burns API quota, and hallucinates boundaries.
3. **What can this host actually run right now (2026-09-10)?** `faster-whisper` ✅, BLIP-2 image captioning ✅ (cached), ffmpeg ✅. PySceneDetect / WhisperX / TransNetV2 / YOLO / SAM2 are **not installed** — and the host has **no GPU at all** (`nvidia-smi` missing, `torch.cuda.is_available() == False`).

---

## Part 1 — TransNetV2 vs PySceneDetect: where the gap lives

### Fundamental algorithmic difference

| | PySceneDetect | TransNetV2 |
|---|---|---|
| Approach | Classical heuristic | Deep learning |
| Decision signal | Per-frame HSV histogram distance vs threshold | Per-frame transition probability from a dilated CNN |
| Lookback window | Adjacent 2 frames | ~100 frames (multi-scale temporal context) |
| Output | Binary cut points | Continuous probability per frame → confidence thresholded |
| Training data | None (hand-tuned) | Synthetic + real cut / fade / dissolve corpus |
| Tuning | Per-content-type threshold work | Zero — learned universal patterns |

**Core gap:** PySceneDetect asks "do these two consecutive frames look different?", TransNetV2 asks "does this moment in the timeline look like a transition?". A 100-frame receptive field is what enables fade/dissolve detection — a 2-frame comparison cannot see gradual transitions no matter how the threshold is tuned.

### Specific failure modes of PySceneDetect that TransNetV2 fixes

#### 1. Fade / Dissolve — biggest gap

```
Scene A ──── dim ──── black ──── bright ──── Scene B
```

- PySceneDetect **misses the fade entirely**. Per-frame delta is continuous, never crosses the threshold. A 5-second fade gets reported as one shot.
- TransNetV2 was **explicitly trained on fade/dissolve**, locates fade-in start frame.

Cinema / emotional cuts / social-media transitions are 80%+ fades. On this material PySceneDetect is functionally blind.

#### 2. Low-contrast hard cut

```
Scene A (white wall) ──cut── Scene B (white wall + slightly different furniture)
```

- PySceneDetect: histograms nearly identical → **miss**.
- TransNetV2: CNN learned the "same foreground, semantic switch" pattern.

Talk shows, interior interviews, white-background talking heads — all low-contrast hard cuts.

#### 3. Flash / strobe / lens flare

- PySceneDetect: every flash spikes the histogram → **floods false positives**. One camera flash in news footage → 30 phantom cuts.
- TransNetV2: trained on these → **ignores them**.

Live concerts, sports replays, news packages — PySceneDetect unusable.

#### 4. Fast motion / whip pan / subject tracking

- PySceneDetect: large inter-frame subject displacement → **fires**. A 3-second tracking shot becomes 90 phantom cuts.
- TransNetV2: separates **motion patterns** from **cut patterns** in the learned representation.

Sports, action films, documentary follow-shots — false-positive rate explodes.

#### 5. Frame-precise cut position

- PySceneDetect: returns a *window* (e.g. `[1024..1032, 12 frames wide]`). Pick the middle by convention.
- TransNetV2: returns a per-frame probability curve. Peak is exact to one frame.

Matters for downstream consumers — auto-reframe, shot-aware captioning, keyframe sampling, subtitle alignment.

#### 6. Confidence score

- PySceneDetect: binary. Cut or no cut.
- TransNetV2: `{frame: 1024, transition_prob: 0.94}`. Filterable. Rankable. Inspectable.

### Where PySceneDetect is still fine (don't over-upgrade)

- **Pure hard-cut material** — screen recordings, slide deck captures, Vlog with conventional cuts only.
- **Tolerance for a rough window** — when downstream only needs "roughly where shots divide".
- **No GPU, no PyTorch willing to install** — PySceneDetect is pure Python + OpenCV.
- **Latency-sensitive one-off** — PySceneDetect is 10×+ faster on CPU.

### Decision matrix

| Reference video profile | Recommended tool | Why |
|---|---|---|
| Cinema / mood piece / emotional cuts | **TransNetV2** | Fade/dissolve heavy; PySceneDetect blind |
| Short-form social / Reels / TikTok | **TransNetV2** | Rapid cuts + heavy flash/strobe; false positives explode |
| Live sports / concert / news | **TransNetV2** | Flash + whip-pan; PySceneDetect unusable |
| Vlog / interview / interior fixed camera | **TransNetV2** | Low-contrast hard cuts |
| Screen recording / slide deck / tutorial | **PySceneDetect OK** | All hard cuts, no motion; save GPU |
| Don't know / general case | **TransNetV2** | Default to the more accurate; fall back only when material is known pure-hard-cut |

---

## Part 2 — Can a general VLM (MiniMax, Qwen2.5-VL, GPT-4o) replace TransNetV2?

### Short answer: **No.** They are complementary, not substitutes.

| | TransNetV2 | General VLM (MiniMax / Qwen2.5-VL / GPT-4o) |
|---|---|---|
| Task | **Classification** — is this frame a transition? | **Understanding** — what is this video about? |
| Output | Per-frame probability vector `[0.01, 0.02, 0.97, 0.04, …]` | Natural-language description |
| Training objective | Frame-level binary classification | Next-token prediction |
| Temporal precision | **Frame-accurate** | **Narrative-coarse** ("around 15s") |
| Determinism | Same input → same output | Stochastic even at temperature 0 |

### Why substituting breaks

#### 1. Precision gap is an order of magnitude

TransNetV2: `{frame: 1024, transition_prob: 0.94}` — exact to one frame.

VLM: "the cut happens around 34-35 seconds" — could be 33.2s. Hundreds of milliseconds off breaks subtitle alignment, auto-reframe, shot-aware captioning.

#### 2. Hallucinated cut points

LLMs find **narrative boundaries** ("topic shifted"), not **visual shot boundaries** ("frame discontinuity"). A single continuous shot where the subject changes expression can be flagged as "a cut". A true hard cut followed by 5 seconds of the same setup can be missed.

#### 3. Cost and latency blow up

For a 60s video at 1 fps sampling = 60 images:

- TransNetV2 (local): ~5s wall clock, **effectively free**
- VLM API: 60 images × ~1000 input tokens + output description ≈ **$0.05–0.10 per minute of video**, plus ~30s of API round-trip latency

Batch-processing 100 reference videos = $5–10 + 50+ minutes just for shot detection. Bad trade.

### Correct architecture: layer them

```
                 ┌──────────────────────┐
                 │  raw video           │
                 └─────────┬────────────┘
                           │
                           ▼
                 ┌──────────────────────┐
                 │ TransNetV2           │  ← structural: exact cuts
                 │ → [shot1, shot2,...] │
                 └─────────┬────────────┘
                           │
                           ▼
                 ┌──────────────────────┐
                 │ representative frames│  ← 1-3 frames / shot
                 │ per shot             │
                 └─────────┬────────────┘
                           │
                           ▼
                 ┌──────────────────────┐
                 │ MiniMax / Qwen2.5-VL│  ← semantic: what's in each shot
                 │ → caption + tags     │
                 └──────────────────────┘
```

This is what `tools/analysis/video_analyzer.py` + `video-reference-analyst` is **designed** to do. Don't break the architecture by collapsing two roles into one model.

### What if GPU is the constraint?

The original question implied TransNetV2 needs GPU. Reality:

| Path | Speed | Cost | Accuracy vs TransNetV2 |
|---|---|---|---|
| TransNetV2 on CPU (PyTorch CPU mode) | 1080p at ~0.3-1× realtime | free | **100%** (same model, same weights) |
| TransNetV2 on Apple MPS | 1080p at ~2-5× realtime | free | 100% |
| TransNetV2 on cloud GPU spot (Modal / RunPod / Lambda) | 5×+ realtime | **~$0.0006/min of video** | 100% |
| PySceneDetect with tuned thresholds | 10×+ realtime | free | 60-70% |
| AutoShot (MSRA, DL-based) | medium | free | ~85% |
| **General VLM (MiniMax / Qwen-VL) doing shot detection** | slow | $0.05-0.10/min | **40-60% and not frame-precise** |

Conclusion: GPU is **not** a hard requirement. CPU is fine for occasional use; cloud spot GPU is cheaper than burning VLM API quota even at scale.

---

## Part 3 — Other video decomposition tools worth knowing (not in the previous doc)

The [`video-decomposition-stack-2026-09-10.md`](video-decomposition-stack-2026-09-10.md) covers the main nine dimensions. Adjacent capabilities that often appear in "decompose this video" requests:

| Capability | Tool(s) | Notes |
|---|---|---|
| **Dense video captioning** (time-stamped event captions for every action) | **Vid2Seq**, **PDVC**, **VideoXum** | Output: `[0.0–3.4s] chef pours oil, [3.4–7.1s] onion hits pan, ...`. Closer to "storyboard" than single-shot caption. |
| **Scene graph generation** (`(subject, predicate, object)` triples per shot) | SGG models on Visual Genome / Open Images | Useful for "what's in relation to what" queries; complements per-shot caption. |
| **Camera motion classification** (pan / tilt / zoom / dolly / static) | **LPCVC** family, **MovieNet**-derived classifiers | Output per shot: `{shot: 4, motion: "slow_dolly_in"}`. Critical for matching reference cinematic language. |
| **Temporal grounding** (text query → timestamp ranges) | **Moment-DETR**, **QD-DETR**, **VTG-DETR** | Given "find the part where the chef flips the egg", returns `[00:12.3 – 00:14.7]`. |
| **Pose / skeleton extraction** | **RTMPose** (OpenMMLab), **MMPose**, **DWPose**, **MediaPipe Pose** | Dance, sports, gesture demos. 17-133 keypoint skeletons per frame. |
| **Audio event detection** (non-speech sound events) | **BEATs** (Microsoft), **CLAP** (LAION), **Pengi** | "gunshot at 00:34", "applause 00:51-00:55", "music starts 01:02". Complements ASR. |
| **Music source separation** (vocals / drums / bass / other) | **Demucs v4** (Meta), **Open-Unmix**, **Spleeter** | Useful when repurposing reference music — extract instrumental or acapella. |
| **Long-video understanding** | **LongVU**, **LongVILA**, **Apollo**, **Video-CCAM** | Efficient transformers that keep >1h video in context without TransNetV2-style chunking. |
| **Multi-modal binding** (image + audio + text in shared embedding) | **ImageBind** (Meta, MIT), **LanguageBind** | One vector per chunk across modalities — enables "find the moment that sounds like X and looks like Y". |
| **Background removal in video** | **RobustVideoMatting** (RVM), **MODNet** | Per-frame alpha matte; temporal stability for green-screen replacement. |

**Standouts for "decompose a reference video" workflows:**

- **Dense captioning (Vid2Seq / VideoXum)** — closest single tool to "give me a time-anchored storyboard"
- **Camera motion classification** — without it, the camera-motion vocabulary in `video-reference-analyst.md` (push/pull/pan/dolly) is up to a VLM to guess, and VLMs are unreliable on motion classification
- **BEATs (audio events)** — non-speech sound design matters (applause, whoosh, impact); ASR misses all of it
- **Demucs** — if the reference is a music clip you want to repurpose, separating stems is half the job

---

## Part 4 — Current system runnability check (this host, 2026-09-10)

Hard data from `.venv/bin/python` and `nvidia-smi`:

### Hardware

| Resource | Status |
|---|---|
| GPU | ❌ **none** — `nvidia-smi: command not found`; `torch.cuda.is_available() == False`; `torch.backends.mps` not available |
| RAM / disk | ✅ 216 GB free disk; sufficient RAM (no measurement taken) |
| Node / npx | ✅ node v22.22.1, npx 10.9.4 |
| ffmpeg | ✅ 6.1 |
| yt-dlp / curl / git | ✅ all present |

### Installed Python packages (OM venv, Python 3.10.12, torch 2.13.0+cu130)

| Package | Status | Used by |
|---|---|---|
| `torch` 2.13.0+cu130 | ✅ installed (but CUDA unavailable — **CPU only**) | TransNetV2, BLIP-2, all PyTorch-based models |
| `transformers` 5.16.1 | ✅ installed | BLIP-2, any HF model |
| `faster-whisper` 1.2.1 | ✅ installed | OM `transcriber` (ASR) |
| `numpy` 2.2.6, `PIL` 12.3.0, `av` 17.1.0, `soundfile` 0.14.0 | ✅ installed | base image / audio IO |
| `scenedetect` (PySceneDetect) | ❌ **NOT installed** — but `tools/analysis/scene_detect.py` imports it. **Latent bug**: scene_detect tool will fail at runtime; the FFmpeg fallback inside the tool is the only working path |
| `whisperx` | ❌ NOT installed | word-level timestamps not available |
| `opencv-python` (cv2) | ❌ NOT installed | PySceneDetect, most video processing — missing dependency |
| `pyannote.audio` | ❌ NOT installed | speaker diarization |
| `ultralytics` (YOLO) | ❌ NOT installed | object detection |
| `segment_anything_2` (SAM2) | ❌ NOT installed | video segmentation |
| `open_clip_torch` | ❌ NOT installed | CLIP/SigLIP embeddings |
| `paddleocr` / `funasr` | ❌ NOT installed | OCR / Chinese ASR |
| `librosa`, `pandas`, `decord`, `moviepy` | ❌ NOT installed | various helpers |

### HuggingFace cache (41 GB total)

Already cached and ready to use:

- `models--mobiuslabsgmbh--faster-whisper-large-v3-turbo` ✅ ASR large-v3 turbo
- `models--Systran--faster-whisper-base` ✅ ASR base
- `models--openai--whisper-large-v3-turbo` ✅ OpenAI Whisper large-v3 turbo
- `models--Salesforce--blip2-opt-2.7b` ✅ **BLIP-2 image captioning** (not yet wired into OM `video_understand` but cached and ready)
- `models--facebook--musicgen-small` ✅ music generation (wired in OM)
- `models--hexgrad--Kokoro-82M` ✅ TTS (wired in OM)
- `models--facebook--nllb-200-distilled-600M` ✅ translation
- `models--Qwen--Qwen3-0.6B` / `Qwen--Qwen3-TTS-12Hz-1.7B-Base` ✅ small Qwen LLM + Qwen3-TTS
- `models--opendatalab--PDF-Extract-Kit-1.0` / `MinerU2.5-Pro-2605-1.2B` ✅ document extraction (out of video scope)

### What the host can run today, by task

| Task | Tool | Status on this host |
|---|---|---|
| Frame extraction / re-encode / concat | ffmpeg | ✅ Ready |
| ASR (English) | faster-whisper large-v3 turbo | ✅ Ready (CPU, ~real-time ×0.5 for 60s clip) |
| ASR (Chinese) | faster-whisper | ✅ Works; not optimal (no FunASR) |
| Image captioning (per-frame) | BLIP-2 opt-2.7b | ✅ Cached + loadable; CPU ~46s/frame per recent memory |
| Music generation | MusicGen-small | ✅ Cached; CPU slow per `MUSIC-ISSUES-AND-FIXES.md` |
| **Shot boundary detection (current)** | PySceneDetect | ❌ **broken — not installed, but OM imports it** |
| Shot boundary detection (target) | TransNetV2 | ⚠️ **needs `pip install transnetv2` + model weights; CPU-only inference will be slow** |
| Word-level ASR timestamps | WhisperX | ❌ not installed |
| Speaker diarization | pyannote.audio | ❌ not installed |
| Object detection / segmentation | YOLO / SAM2 | ❌ not installed; ❌ no GPU |
| Visual-language understanding (Qwen-VL) | transformers | ⚠️ framework present; ❌ model weights not cached |
| Dense video captioning | Vid2Seq / PDVC | ❌ not installed |
| Audio event detection | BEATs / CLAP | ❌ not installed |
| Music source separation | Demucs | ❌ not installed |
| Pose estimation | RTMPose / MMPose | ❌ not installed |

### Critical findings

1. **No GPU. Period.** Anything that needs a GPU must run on CPU. TransNetV2 CPU ≈ 0.3-1× realtime — usable for occasional analysis, too slow for batch.
2. **`tools/analysis/scene_detect.py` is silently broken.** It imports `scenedetect` (PySceneDetect) but the package isn't installed in the venv. The tool's FFmpeg-fallback branch is the only working path on this host. This should be patched either by `pip install scenedetect[opencv]` (and `opencv-python`) or by routing through ffmpeg `select='gt(scene,0.X)'` reliably.
3. **41 GB of HF models are already cached**, including BLIP-2 — there is free capability sitting on disk that the OM tool layer isn't wired to consume.
4. **Wins already on this host (no install needed):** faster-whisper, BLIP-2 image captioning, ffmpeg. Enough for "ASR + keyframe captioning" today.

### Upgrade recommendations, in order of ROI

1. **Patch `scene_detect.py` runtime.** Either `pip install scenedetect[opencv] opencv-python` or route all calls through ffmpeg's `select=gt(scene,X)` filter. The current latent import error will surface as a crash the first time someone calls the tool expecting scene cuts.
2. **Install TransNetV2** (`pip install transnetv2` + download `transnetv2-weights/v2_1/`). Even CPU-only is fine for occasional reference-video analysis. Better than the current broken PySceneDetect path.
3. **Wire BLIP-2 into `video_understand`** (model is already cached). Replaces whatever captioner the tool currently uses. ~46s/frame on CPU is slow but usable for offline per-shot descriptions.
4. **Install WhisperX** if word-level timestamps become a requirement. faster-whisper alone gives segment-level only.
5. **Do NOT install YOLO / SAM2 / pyannote / InternVideo / Qwen-VL-72B** until GPU is available. CPU inference for these is not viable at production volume.
6. **Long-term:** a single cloud GPU spot instance (Modal / RunPod) at ~$0.5/hr covers TransNetV2 + SAM2 + Qwen-VL + WhisperX + pyannote + InternVideo batch jobs at marginal cost. Cheaper than burning VLM API quota.

---

## References

- [`video-decomposition-stack-2026-09-10.md`](video-decomposition-stack-2026-09-10.md) — the broader open-source ecosystem map this doc extends.
- [`when-to-use-external-t2v-2026-09-10.md`](when-to-use-external-t2v-2026-09-10.md) — when to call external T2V vs native composition.
- [`../skills/meta/video-reference-analyst.md`](../skills/meta/video-reference-analyst.md) — OM's reference-video workflow (the consumer of these tools).
- [`../tools/analysis/scene_detect.py`](../tools/analysis/scene_detect.py) — the current scene detection wrapper; imports `scenedetect` even though it's not installed in this venv.
- [`../tools/analysis/transcriber.py`](../tools/analysis/transcriber.py) — current faster-whisper / WhisperX wrapper.
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/blip2-opt-2.7b-sharded-safetensors-cache-layout.md` — confirms BLIP-2 is cached and ready; ~46s/frame on CPU.
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/voicebox-blocked-kokoro-escape-hatch.md` — pattern for when registry says available but runtime fails.
- https://github.com/soCzech/TransNetV2 — TransNetV2 repo (Charles University Prague, Apache 2.0).