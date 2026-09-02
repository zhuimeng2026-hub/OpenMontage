# 音频生成 / 转码工具负载分析（详细）

**日期**：2026-09-01
**范围**：`tools/audio/`（20+ 个 TTS / 音乐 / 混音 / FunASR 工具）+ `tools/subtitle/` + `tools/translation/` + `tools/analysis/` 转录

---

## 1. TTS（文本转语音）

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `dashscope_tts.py` | **API** | 网络+256MB RAM | 无 | — | 5–15s | **高** — 密钥由服务端管理最合理 |
| `doubao_tts.py` | **API** | 网络+256MB RAM | 无 | — | 8–30s | **高** — 异步云 API |
| `edge_tts.py` | **API** | 网络+256MB RAM | 无（pip `edge-tts`） | — | 2–8s | **高** — 免费但受限流影响 |
| `elevenlabs_tts.py` | **API** | 网络+256MB RAM | 无（含 clone_voice） | — | 5–20s | **高** — 付费云 API |
| `google_tts.py` | **API** | 网络+256MB RAM | 无 | — | 3–10s | **高** — Google Cloud TTS |
| `kling_tts.py` | **API** | 网络+256MB RAM | 无 | — | 20–60s | **高** — 官方 Kling API |
| `openai_tts.py` | **API** | 网络+256MB RAM | 无 | — | 3–10s | **高** — OpenAI TTS |
| `piper_tts.py` | **LOCAL** | CPU 2c / 512MB / 0 VRAM | Piper ONNX | **15–50 MB/voice** | 1–3s/句 | **高** — 强推客户端 |
| `kokoro_tts.py` | **LOCAL** | CPU 4c / 2GB / 0 VRAM | Kokoro-82M | **~330 MB** | 0.5–2s/句 | **高** — 强推客户端 |
| `voicebox_tts.py` | **LOCAL**（REST 代理） | 转发至本地 Voicebox 后端 | Qwen3-TTS 0.6B/1.7B | **3–4 GB** | 10–60s/段 | **中** — 强机客户端可执行 |
| `tts_selector.py` | **HYBRID** | 仅 64MB RAM，纯路由 | 无 | — | 调度开销 | **高** — 元控制器，留服务端 |

## 2. 音乐生成 / 检索

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `freesound_music.py` | **API** | 网络+256MB RAM | 无 | — | 5–15s | **高** — 无密钥 |
| `pixabay_music.py` | **API**（抓取） | 网络+256MB RAM | 无 | — | 5–20s | **中** — 反爬风险 |
| `suno_music.py` | **API** | 网络+256MB RAM | 无 | — | 30–300s | **高** — 付费云 API |
| `music_gen.py` (ElevenLabs) | **API** | 网络+256MB RAM | 无 | — | 10–60s | **高** — 付费云 API |
| `google_music.py` | **API** | 网络+256MB RAM | 无 | — | 15–60s | **高** — Google Lyria |
| `music_gen_local.py` | **LOCAL** | CPU 2c / 4GB RAM / **2GB VRAM** | MusicGen | small **~300 MB**；large **~3.3 GB** | CPU small ~30s；GPU small ~3–10s | **中** — small 可客户端 |
| `music_library.py` | **LOCAL** | 64MB RAM，纯 fs scan | 无 | — | <1s | **高** — 只读本地目录，客户端执行 |

## 3. 音频增强 / 混音

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `audio_enhance.py` | **LOCAL** (FFmpeg) | CPU 1c / 512MB / 0 VRAM / 500MB 临时盘 | 无（可选 pedalboard） | — | 1–3× 时长 | **高** — CPU 实时，强推客户端 |
| `audio_mixer.py` | **LOCAL** (FFmpeg) | CPU 2c / 1GB / 0 VRAM / 500MB 临时盘 | 无 | — | 1–3× 时长 | **高** — CPU 实时，强推客户端 |

## 4. 字幕生成

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `subtitle_gen.py` | **LOCAL** | CPU 1c / 128MB / 0 VRAM / 10MB 盘 | 无（纯 Python） | — | <100ms | **高** — 强推客户端 |

## 5. 翻译

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `argos_translator.py` | **LOCAL** | CPU 2c / 1GB / 0 VRAM / 500MB 盘 | Argos CTranslate2 | **~100–200 MB / 语言对** | 0.5–2s/段 | **高** — 强推客户端 |
| `nllb_translator.py` | **LOCAL** | CPU 2c / 3GB RAM / **2GB VRAM** | NLLB-200 | 600M **~2.4 GB**；1.3B **~5 GB**；3.3B **~13 GB** | CPU 600M 5–15s/段 | **中** — 600M 可客户端 |
| `translator.py` | **HYBRID** | 64MB RAM，纯调度 | 无 | — | 调度开销 | **高** — 路由器，留服务端 |

## 6. 转录（STT）

| 工具 | runtime | 主消耗 | 本地模型 | 模型大小 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `transcriber.py` | **LOCAL** | CPU 2c / 2GB RAM / 0 VRAM | faster-whisper | tiny **~40 MB**；large-v3 **~3 GB** | base CPU ~0.5×实时；large-v3 GPU ~0.05–0.1× | **中–高** — tiny/base/small 客户端 |
| `funasr_transcriber.py` | **LOCAL** | CPU 2c / 2GB RAM / 0 VRAM / 800MB 盘 | FunASR Paraformer | paraformer-zh **~400 MB** | CPU ~0.3–0.5×实时 | **高** — 中文专精，强推客户端 |
| `azure_stt.py` | **API** | 网络+256MB RAM | 无 | — | 5–30s/小时音频 | **高** — 付费云 STT |
| `dashscope_asr.py` | **API** | 网络+256MB RAM | 无（Qwen3-ASR） | — | 30–120s | **高** — 阿里云 ASR |

---

## 关键结论

### A. 留在服务端（云端完成，纯 API 编排）

**TTS**：`dashscope_tts`、`doubao_tts`、`elevenlabs_tts`、`google_tts`、`kling_tts`、`openai_tts`、`edge_tts`（IP 限流）
**音乐**：`suno_music`、`music_gen`（ElevenLabs）、`google_music`、`freesound_music`、`pixabay_music`
**STT**：`azure_stt`、`dashscope_asr`
**路由器**：`tts_selector`、`translator`

### B. 剥离到客户端（本地模型）

- **TTS**：`piper_tts`（15–50MB，CPU 实时）→ **强推**；`kokoro_tts`（82M，~330MB，CPU 实时）→ **强推**
- **音乐**：`music_gen_local`（small 模型 ~300MB，可 CPU）→ **中推**
- **混音 / 增强**：`audio_mixer`、`audio_enhance` → **强推**
- **字幕**：`subtitle_gen` → **强推**
- **音乐库扫描**：`music_library` → **强推**
- **翻译（轻量）**：`argos_translator`（~200MB）→ **强推**
- **STT（轻量）**：`funasr_transcriber`（~400MB，中文最优）→ **强推**；`transcriber` (tiny/base/small) → **强推**

### C. HYBRID（视客户端硬件）

- **`voicebox_tts`**：客户端 GPU 强机跑 Qwen3-TTS 1.7B；弱机降级 Kokoro
- **`nllb_translator`**：600M 客户端 CPU；1.3B/3.3B 服务端
- **`transcriber` (large-v3/turbo)**：客户端有 GPU 本地；否则回落 base/small 或切 Azure STT

### D. 本地模型清单（量化）

| 模型 | 大小 | 客户端可行性 |
|---|---|---|
| Piper | 15–50 MB/voice | 1GB 内存稳跑 |
| Kokoro | ~330 MB | 1GB 内存稳跑 |
| MusicGen-small | ~300 MB | 2GB 内存稳跑 |
| FunASR paraformer-zh | ~400 MB | 1GB 内存稳跑 |
| faster-whisper base | ~150 MB | 1GB 内存稳跑 |
| Argos en↔zh | ~200 MB | 1GB 内存稳跑 |
| NLLB-600M | ~2.4 GB | 4GB 内存稳跑 |
| Voicebox (Qwen3-TTS 1.7B) | ~3–4 GB | 需客户端 GPU |
| NLLB-3.3B | ~13 GB | 强机客户端或服务端 |

---

## 设计层面关键启示

1. **`runtime=API` 字段就是切分金标准**。selector 留服务端做编排，LOCAL 工具暴露给客户端调用。
2. **隐私敏感工具几乎都已就位**：`piper_tts`、`kokoro_tts`、`voicebox_tts`、`music_gen_local`、`funasr_transcriber`、`transcriber`（offline 模式）、`argos_translator`、`nllb_translator` 均声明 `supports.offline=True + network_required=False`，可直接客户端执行。
3. **真正的客户端杀手**：所有「FFmpeg-only」工具（`audio_enhance`、`audio_mixer`）和「纯 Python」工具（`subtitle_gen`）无任何理由留在服务端——它们是 I/O bound 而非 compute bound，留服务端只会增加带宽成本。
