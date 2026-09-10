# Video decomposition — strongest open-source tool stack (2026-09-10)

> **Status:** stable reference. Map the open-source ecosystem of video-decomposition tools by capability dimension. Pair with [`when-to-use-external-t2v-2026-09-10.md`](when-to-use-external-t2v-2026-09-10.md) — that doc decides WHEN to use external T2V; this doc decides what to use AFTER you've decided to actually decompose a reference video.
> **Audience:** agents and humans running `video-reference-analyst` or any "watch this video and tell me what's going on" workflow.
> **Date:** 2026-09-10.

## TL;DR

**There is no single "strongest" tool.** Decomposition has at least six independent capability dimensions, each with its own SOTA. The strongest production stack is an **orchestration** of specialized tools, not a single model.

| Dimension | Open-source SOTA | Why |
|---|---|---|
| Shot boundary detection | **TransNetV2** | ClipShots/RAI benchmark #1; handles fades/dissolves where PySceneDetect fails |
| ASR + word-level timestamps | **WhisperX** (en) / **FunASR** (zh) | Whisper + wav2vec2 forced alignment; 5-10% lower WER than Whisper alone |
| Speaker diarization | **pyannote-audio 3.1** | DER ~0.5; runs on CPU |
| Visual semantic understanding | **Qwen2.5-VL-72B** ≈ **InternVideo2.5** | Top of VideoMME/MVBench/MLVU; close the gap to closed-source |
| Object / person segmentation & tracking | **SAM2** | SA-V benchmark #1; closed-source has not caught up |
| Visual feature embedding | **SigLIP-2** / **DINOv2** | Best cross-modal and self-supervised features respectively |

On the closed-source side, **Gemini 2.0 Pro** and **GPT-4o** still lead on whole-video semantic comprehension, but open-source has **closed the gap from a generation down to roughly one tier**, and **leads on structured decomposition** (shot boundaries, word-level timestamps, segmentation).

## The dimension-by-dimension map

### 1. Shot boundary detection (镜头切分)

- **TransNetV2** — Apache 2.0. Authors: Tomáš Souček, Jakub Lokoč (Charles University Prague, SIRET group). Repo: https://github.com/soCzech/TransNetV2 . Deep network trained on synthetic+real cut data. Robust on hard cuts, fades, dissolves. ~80MB model. **Local inference, not a hosted service.**
- **PySceneDetect** — BSD-3. Most popular. Classical threshold/content/adaptive detectors. Fast, easy to tune. Lower accuracy on hard cuts.
- **AutoShot** (Microsoft Research, 2021) — DL-based alternative.
- **Recommendation:** TransNetV2 when accuracy matters; PySceneDetect when speed/ease matters.

> **"Service or software?" — TransNetV2 is open-source software** with pretrained weights. Not a SaaS. Same category as PySceneDetect / YOLO / SAM2. Can be wrapped as a service (e.g. on Replicate) but the project itself ships as code + weights for local inference.

### 2. ASR + word-level timestamps

- **Whisper large-v3 / turbo** (OpenAI, MIT) — multilingual SOTA, ~100 languages.
- **faster-whisper** — CTranslate2 port; same model, 4-5× faster. Production default.
- **WhisperX** — Whisper + wav2vec2 forced alignment. Word-level timestamp precision <50ms drift. **The de facto standard for subtitle / alignment use cases.**
- **FunASR / Paraformer-large** (Alibaba, MIT) — **Chinese SOTA**; 5-15 WER points below Whisper on `zh`. Supports `en` too.
- **Canary** (NVIDIA NeMo, 2024) — strong multilingual.
- **Distil-Whisper** — 6× faster, ~1 WER accuracy loss.

### 3. Speaker diarization (who is speaking)

- **pyannote-audio 3.1** (MIT) — open-source SOTA, runs on CPU. Needs HF token for gated weights.
- **NeMo Sortformer / MSDD** (NVIDIA) — 2024 new entrant; better for call-center / meeting audio.

### 4. Visual semantic understanding ("what is happening")

This is the dimension that moved fastest from 2024–2026. SOTA migrated from specialized models to general-purpose multimodal LLMs.

| Model | Source | Strengths | Weaknesses |
|---|---|---|---|
| **Qwen2.5-VL-72B** | Alibaba (Apache 2.0) | Hour-long video natively, multi-resolution, temporal grounding, strong OCR | Needs good GPU for 72B |
| **InternVideo2.5** | Shanghai AI Lab (MIT) | VideoMME / MVBench / MLVU leader, best action understanding | Shorter context window |
| **VideoLLaMA3** | DAMO Academy (2025) | Audio+visual joint | Slightly behind on benchmarks |
| **LLaVA-Video** | Bytedance | Strong instruction following, fine detail | Long-video weak |
| **MiniCPM-V 2.6 / 3.0** | OpenBMB (Apache 2.0) | **Lightweight** (≤8B), fast | Complex scenes: precision drops |
| **CogVLM2-Video** | Zhipu | Chinese-friendly | Maintenance spotty |
| **VideoChat2** | Shanghai AI Lab (MIT) | Veteran, stable | Superseded by newer |

**Recommendation:**
- Resources available → **Qwen2.5-VL-72B or InternVideo2.5**. Pick by workload: long video + OCR → Qwen2.5-VL; action-heavy event understanding → InternVideo2.5.
- Resources tight → **MiniCPM-V 2.6** or **Qwen2.5-VL-7B**. Trade accuracy for speed.
- Maximum quality (closed) → **Gemini 2.0 Pro / GPT-4o / Claude Sonnet 4.5**. Still ~one tier ahead on aesthetic/holistic comprehension, but data leaves your host.

### 5. Object / person detection & segmentation / tracking

- **SAM2** (Meta, Apache 2.0, 2024) — "Segment Anything in Videos". **Open-source SOTA** on SA-V benchmark. Give it one frame + a point → tracks the object across the whole video with masks. **Closed-source has not caught up on this specifically.**
- **YOLO11 / YOLO12** — real-time detection, speed king.
- **Grounding DINO + SAM** — text → bbox → mask; the classic open-vocabulary detection pipeline.
- **Florence-2** (Microsoft, MIT) — detection + captioning joint; 2024 new entrant.
- **Depth Anything V2** / **Depth Pro** (Apple) — monocular depth; useful for 3D-structure understanding.

### 6. Visual feature embedding (cross-shot retrieval, deduplication)

- **SigLIP-2** (Google, Apache 2.0) — vision-language alignment SOTA; better than CLIP on retrieval/classification.
- **DINOv2** (Meta, Apache 2.0) — pure self-supervised features; best for visual similarity / clustering without text.
- **PerceptionEncoder** (Meta, 2024) — CLIP + DINO joint distillation.
- **CLIP** (OpenAI, MIT) — the classic. **Strictly superseded by SigLIP / DINOv2** in most production scenarios; worth replacing if you still use it.

### 7. OCR in video

- **GOT-OCR2.0** (2024) — current strongest open-source OCR. Arbitrary resolution, multilingual, math + tables.
- **PaddleOCR** (Apache 2.0) — **Chinese SOTA**, English solid, best production engineering.
- **Surya** (2024) — document-level OCR strong.
- **EasyOCR** — lightweight, but accuracy mediocre.

### 8. Action recognition (fine-grained classification)

- **InternVideo2** (also covers this dimension)
- **VideoMamba** (2024) — state-space model; **best efficiency/accuracy trade-off**.
- **MMAction2** (OpenMMLab) — comprehensive toolkit; many models, individual accuracy is fair.
- **Video-Swin Transformer** — classic backbone.

### 9. Optical flow / motion analysis

- **RAFT** (MIT) — classic SOTA. Still first-tier precision.
- **GMFlow** (2024) — global matching transformer; faster.
- **SlowFlow** (2024) — lightweight.

---

## The strongest orchestrated stack

```
0. ffmpeg + PyAV                              # frame sampling, audio separation, metadata
1. TransNetV2                                 # shot boundaries
2. WhisperX           (en)
   FunASR/Paraformer  (zh)                    # ASR + word-level timestamps
3. pyannote-audio 3.1                          # speaker diarization
4. SAM2                                       # cross-frame object/person segmentation + tracking
5. YOLO11 + Grounding DINO                    # detection (open + closed vocab)
6. SigLIP-2 + DINOv2                          # per-frame features (cross-modal + pure visual)
7. Qwen2.5-VL-72B   (or InternVideo2.5)       # per-shot semantic caption, action understanding, temporal grounding
8. GOT-OCR2 / PaddleOCR                       # on-screen text
9. (optional) VideoMamba / MMAction2          # fine-grained action classification
```

This stack outputs, for any input video:

- **Timeline:** shot list + word-level ASR + speaker labels + subject tracks (mask + bbox per frame)
- **Content layer:** per-shot semantic description, action classification, on-screen text, object inventory
- **Feature layer:** per-frame SigLIP-2 / DINOv2 embeddings (for cross-video retrieval / dedup)
- **Metadata:** pacing (shot density), visual complexity, camera-physics cues (push/pull/pan/dolly)

This is exactly the structured output the [`video-reference-analyst`](../skills/meta/video-reference-analyst.md) workflow is supposed to produce.

---

## Open-source vs closed-source comparison

| Task | Open-source leader | Closed-source leader | Gap |
|---|---|---|---|
| Hour-long whole-video understanding | Qwen2.5-VL-72B (hour support is a 2025 selling point) | Gemini 2.0 Pro (native hour-long) | Gemini slightly ahead, but the gap shrank from a generation (2024) to one tier (2026) |
| Single-shot fine-grained caption | InternVideo2.5 | GPT-4o / Claude Sonnet 4.5 | Closed still one tier ahead, mainly on detail/aesthetic |
| Word-level timestamps | WhisperX | OpenAI Whisper API | **Open-source leads** (WhisperX + wav2vec2 alignment) |
| Object tracking + segmentation | SAM2 | — | **SAM2 is open-source SOTA closed-source has not publicly matched** |
| Shot boundary detection | TransNetV2 | — | **No serious closed-source competitor; open-source leads** |

**Takeaway:** By 2026, open-source **leads** on structured decomposition (shot boundaries, word-level alignment, segmentation); closed-source still leads on holistic semantic comprehension and aesthetic description. If "decompose" means "extract a structured time-anchored content map," open-source is sufficient. If it means "explain the emotional arc and visual rhetoric," closed-source still wins.

---

## Where OpenMontage's stack sits in this ecosystem

| OM tool | Current impl | Open-source SOTA | Upgrade ROI |
|---|---|---|---|
| `scene_detect` | PySceneDetect | TransNetV2 | **High** — single drop-in upgrade; meaningful accuracy improvement on fades/dissolves |
| `transcriber` | faster-whisper / WhisperX | Already SOTA | None — already optimal |
| `azure_stt` / `dashscope_asr` | Cloud | Top of cloud-side too | None |
| `face_tracker` | Face-specific | SAM2 (broader) | **Medium** — add SAM2 for non-face objects |
| `frame_sampler` | Frame sampling | Same category | None |
| `video_understand` | Multi-modal caption | Qwen2.5-VL-72B or InternVideo2.5 | **High** — generational jump in description quality |
| `video_analyzer` | OM orchestration | In-house orchestration | Consider whether a framework swap is worth it |

**Highest ROI two-step upgrade:**

1. `scene_detect` → **TransNetV2**
2. `video_understand` → **Qwen2.5-VL-72B** (or InternVideo2.5)

These two moves lift reference-video decomposition quality by one generation.

---

## Service vs software — quick rule of thumb

For any tool in this doc, classify it before quoting:

- **Software + weights, runs locally:** TransNetV2, PySceneDetect, SAM2, DINOv2, SigLIP, YOLO, Whisper models, WhisperX, FunASR, faster-whisper, pyannote-audio, VideoMamba, RAFT, PaddleOCR, GOT-OCR2, MiniCPM-V, Qwen2.5-VL (open weights), InternVideo2.5 (open weights), VideoLLaMA3 (open weights).
- **Hosted API / SaaS:** Hailuo-2.3, Veo, Kling, Sora, Runway, Seedance, Gemini API, OpenAI API, Claude API, Azure Speech-to-Text, DashScope ASR.
- **Hybrid (open weights + optional hosted endpoint):** Some of the above models can be deployed to Replicate / Modal / your own FastAPI. The model itself is software; the deployment is a service.

This is the same distinction `when-to-use-external-t2v-2026-09-10.md` § "Provider menu" draws — it's worth being explicit at proposal time so the user knows whether a quota slot is being burned or a local GPU is being used.

---

## References

- [`when-to-use-external-t2v-2026-09-10.md`](when-to-use-external-t2v-2026-09-10.md) — when to call external T2V vs native composition.
- [`t2v-i2v-multi-image-reference-glossary.md`](t2v-i2v-multi-image-reference-glossary.md) — capability-tier vocabulary.
- [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — full pipeline × offline-capability matrix.
- [`docs/PROVIDERS.md`](PROVIDERS.md) — every paid provider with setup, pricing, free-tier notes.
- [`../skills/meta/video-reference-analyst.md`](../skills/meta/video-reference-analyst.md) — OM's reference-video workflow.
- [`../tools/analysis/scene_detect.py`](../tools/analysis/scene_detect.py) — current PySceneDetect wrapper.
- [`../tools/analysis/transcriber.py`](../tools/analysis/transcriber.py) — current faster-whisper / WhisperX wrapper.
- https://github.com/soCzech/TransNetV2 — TransNetV2 repo (Charles University Prague, Apache 2.0).
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/whisper-availability-frozen-verdict.md` — frozen verdict on faster-whisper availability on this host.