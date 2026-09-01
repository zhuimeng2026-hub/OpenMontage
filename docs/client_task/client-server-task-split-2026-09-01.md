# OpenMontage 服务端/客户端任务拆分评估

**日期**：2026-09-01
**背景**：评估当前系统可完成的任务中，负载最重的是哪些；为引入专用客户端做架构准备——服务端只负责编排脚本，重负载交由客户端执行。
**方法**：5 个 Explore 子 agent 并行分析全部 `tools/` 子目录（video / audio / graphics / character / enhancement / analysis / capture / translation / asset / subtitle），按 `tools/base_tool.py` 的 `ToolRuntime.{LOCAL, LOCAL_GPU, API, HYBRID}` 字段分类评估。

---

## 1. 系统负载全景（按绝对资源消耗排序）

| 排名 | 工具族 | 单次资源量级 | 单次耗时 | 关键瓶颈 |
|---|---|---|---|---|
| 🥇 1 | **Remotion 渲染栈** (`video_compose.py` Remotion 路径 + `remotion-composer/` 870MB + `remotion_caption_burn.py` Remotion 路径) | 0.5–1 GB+ RAM/实例 + 30 个 woff2 字体冷启动 + bundler + headless Chrome | 数分钟–十数分钟 | 单点最大瓶颈。`_get_remotion_render_gate = 2` 已强制并发限流 |
| 🥈 2 | **本地 GPU AI 视频生成**（`ltx_video_local` LTX-2 22B、`comfyui_video` WAN 2.2 14B 工作流、`ltx_video_modal`、`hunyuan_video` 14B、`wan_video` 14B、CogVideoX 5B） | 12–24 GB VRAM + 数分钟/段 | 单段分钟级 | 模型权重本身 10GB+ |
| 🥉 3 | **本地 GPU 视觉模型**（`video_understand` LLaVA-1.5 7B、`comfyui_image` FLUX 2 NVFP4、`local_diffusion` SD 2.1 5GB、`upscale` Real-ESRGAN、`face_restore` CodeFormer/GFPGAN、`lip_sync` Wav2Lip、`talking_head` SadTalker） | 2–8 GB VRAM + GB 级权重 | 图像秒级–分钟级 | 多 GPU 模型共存时显存争抢 |
| 4 | **HyperFrames 渲染**（`hyperframes_compose.py` + `video_compose.py` HF 路径） | Node 22 + npm + FFmpeg | 数分钟 | 必须 Node + npm 工具链 |
| 5 | **本地大模型音频**（`voicebox_tts` Qwen3-TTS 1.7B、`music_gen_local` medium/large、NLLB-1.3B/3.3B、`transcriber` large-v3） | 2–13 GB VRAM/disk | 单段秒级–分钟级 | 弱机客户端不可达 |
| 6 | **Manim 数学动画**（`math_animate`） | Manim + LaTeX 全栈 + 用户 Python 执行 | 5–120s | 含沙箱风险 |
| 7 | **视频逐帧增强**（`upscale` 视频路径 / `green_screen_processor` rembg / `face_enhance` / `eye_enhance`） | 逐帧 × N | 时长 × 0.3–1× | 单任务可放大到小时级 |
| 8 | **屏幕/桌面录制**（`screen_recorder`、`cap_recorder`） | 桌面 framebuffer | 用户操作时长 | 架构上无法服务端 |

---

## 2. 必须保留服务端（重负载 + 沙箱 + 协议）

| 工具 | 保留原因 |
|---|---|
| `video_compose.py` Remotion 路径 + `remotion-composer/` (870MB) + `hyperframes_compose` | 870MB node_modules + headless Chrome + bundler，浏览器无承载方式 |
| `ltx_video_local` (LTX-2 22B) / `ltx_video_modal` / `comfyui_video` (WAN 14B) / `hunyuan_video` / `wan_video` 14B / `cogvideo_video` 5B | 模型权重 + VRAM，客户端无法承担 |
| `video_understand` (LLaVA-1.5 7B / BLIP-2) / `lip_sync` (Wav2Lip) / `talking_head` (SadTalker) | 必须 CUDA 4GB+ VRAM |
| `comfyui_image` (FLUX 2 NVFP4) / `local_diffusion` (SD 5GB) / `upscale` (视频路径) / `face_restore` | 重模型 + 大输入 |
| `math_animate` | Manim + LaTeX + 执行用户 Python 代码（沙箱安全） |
| `tts_selector`、`video_selector`、`image_selector`、`translator` | 元编排路由层，永远留服务端 |
| `asset_upload`、`asset_upload_chunk`、`read_session_asset*`、`rsync_upload` | MCP/BFF 协议入口 |
| 所有付费 API 工具（若选择"服务端代理 key"模式） | key 统一管理 + 配额控制 |

---

## 3. 必须客户端（架构决定，零选择空间）

| 工具 | 原因 |
|---|---|
| `screen_recorder.py` | 抓取本机桌面 framebuffer，离开客户端无意义 |
| `cap_recorder.py` | 桥接桌面端 Cap 应用，路径来自 `AppData`/`Library/Application Support` |
| `screen_capture_selector.py` | 调度器，路由到上述两者 |
| `video_downloader.py` | yt-dlp 服务端下行带宽贵、YouTube 偶拦服务端 IP；客户端零成本 |

---

## 4. 强烈推荐剥离到客户端（高收益、低风险）

### 4.1 FFmpeg-only 工具下沉（FFmpeg.wasm / WebCodecs）

| 工具 | 客户端承载方式 | 释放资源 |
|---|---|---|
| `silence_cutter.py` | FFmpeg.wasm `silencedetect` | 服务端 CPU 长跑 |
| `video_stitch.py` (cut/concat/normalize) | FFmpeg.wasm | 服务端 CPU 长跑 |
| `remotion_caption_burn.py` (FFmpeg 回退路径) | FFmpeg.wasm 烧字幕 | 数分钟 → 数十秒 |
| `video_compose.py` (FFmpeg-only compose/encode/burn_subtitles/overlay) | FFmpeg.wasm / WebCodecs | 服务端 CPU |
| `auto_reframe.py` (静态裁切) | FFmpeg `sendcmd` filter（WebAssembly 版支持） | 服务端 CPU |

### 4.2 音频纯 FFmpeg/纯 Python 工具下沉

| 工具 | 客户端承载方式 |
|---|---|
| `audio_mixer.py` | FFmpeg.wasm `amix` / `sidechaincompress` |
| `audio_enhance.py` | FFmpeg.wasm filter 链 |
| `subtitle_gen.py` | 纯 JS（<100ms 字符串格式化） |
| `music_library.py` | 客户端 fs scan |

### 4.3 本地小模型音频工具下沉（模型随客户端走）

| 工具 | 本地模型 | 模型大小 | 释放 |
|---|---|---|---|
| `piper_tts.py` | Piper ONNX | 15–50 MB/voice | 服务端 CPU + 模型加载 |
| `kokoro_tts.py` | Kokoro-82M | ~330 MB | 服务端模型 + 推理 |
| `music_gen_local.py` (small) | MusicGen small | ~300 MB | 服务端模型 + 推理 |
| `funasr_transcriber.py` | FunASR Paraformer-zh | ~400 MB | 服务端 CPU 推理 |
| `transcriber.py` (tiny/base/small) | faster-whisper | 40MB–500MB | 服务端模型 + 推理；**语音隐私敏感** |
| `argos_translator.py` | Argos CTranslate2 | ~100–200 MB/语言对 | 服务端模型 + 推理 |

### 4.4 视频分析预处理下沉（省 95% 带宽）

| 工具 | 客户端承载方式 | 上行节省 |
|---|---|---|
| `frame_sampler.py` | FFmpeg.wasm + Pillow 等价物 | 只回传关键帧 |
| `visual_qa.py` | FFmpeg.wasm 抽帧 | 不回传整片 |
| `audio_energy.py` | ffmpeg ebur128 WASM | 回传 KB 级 loudness profile |
| `audio_probe.py` | ffprobe WASM | <1s metadata |
| `composition_validator.py` | 纯 JS 校验 + 子 ffprobe | KB 级 JSON |
| `scene_detect.py` | ffmpeg `select` filter（PySceneDetect WASM 备选） | KB 级 scene list |
| `face_tracker.py` | MediaPipe WASM | KB 级 faces JSON |

### 4.5 字符动画 5 个纯 JSON 工具下沉

| 工具 | 客户端承载方式 |
|---|---|
| `character_spec_generator` | 纯 Python 数据变换（<50ms） |
| `svg_rig_builder` | 纯数据变换 |
| `pose_library_builder` | 纯数据变换 |
| `action_timeline_compiler` | 纯数据变换 |
| `character_animation_reviewer` | schema 校验（<100ms） |

### 4.6 轻量渲染类下沉

| 工具 | 客户端承载方式 |
|---|---|
| `code_snippet` | Canvas + shiki/highlight.js（替代 Pygments + Pillow） |
| `color_grade` | WebGL 着色器 / CSS filter |
| `face_enhance` | WebGL smartblur/unsharp/curves |
| `character_rig_renderer` (HTML/GSAP 路径) | 浏览器原生 GSAP |
| `image_selector` | 纯评分路由 |

---

## 5. 可剥离但需轻量重写（中等收益）

| 工具 | 客户端承载方式 | 备注 |
|---|---|---|
| `green_screen_processor.py` chromakey | 重写为单 FFmpeg filtergraph（不要逐帧 ffmpeg 子进程） | 当前逐帧 400+ 子进程开销对浏览器不友好 |
| `green_screen_processor.py` rembg 路径 | onnxruntime-web WASM 跑 u2net 175MB | 需有 GPU 的客户端；纯 CPU 不建议 |
| `bg_remove.py` | ONNX Runtime Web WASM | U²-Net ~170MB 模型；CPU 模式浏览器也跑得动（~2s/张 1080p） |
| `eye_enhance.py` | MediaPipe Face Mesh（WASM/JS 原生支持） | 当前 CPU 实现 |
| `diagram_gen.py` | mermaid.js + 浏览器 SVG | mmdc CLI 难迁，社区有 mermaid.js |
| `character_rig_renderer.py` (MP4 路径) | 浏览器 WebCodecs 替代 Playwright + ffmpeg | — |

---

## 6. 可双跑（视客户端硬件）

| 工具 | 客户端降级方案 | 服务端兜底 |
|---|---|---|
| `wan_video.py` | 1.3B (8GB VRAM) 客户端 | 14B (14GB VRAM) 服务端 |
| `cogvideo_video.py` | 2B (8GB) 客户端 | 5B (12GB) 服务端 |
| `nllb_translator.py` | 600M CPU 客户端（~2.4GB） | 1.3B / 3.3B 服务端 |
| `transcriber.py` (large-v3) | 客户端 GPU | 服务端 |
| `voicebox_tts.py` | 客户端 GPU 强机跑 Qwen3-TTS 1.7B；弱机降级 Kokoro | 服务端 GPU 池 |

---

## 7. API 类视频工具（16 个）—— 关键洞察

> **API 工具的 `resource_profile = cpu=1, ram=512MB, vram=0, network=required`**，瓶颈在**网络和密钥管理**，不在计算。

| 工具 | 客户端直连价值 | 风险 |
|---|---|---|
| `sora_video` (OpenAI SDK) | 高（SDK 原生） | 需 `openai>=2.44.0` 打包 |
| `veo_video` (Google SDK) | 高 | 需 OAuth/ADC |
| `seedance_video/replicate/relay` | 高（fal.ai/中转站/直接） | key 管理 |
| `kling_video / kling_official_video / kling_relay` | 高 | key 管理 |
| `runway_video`、`higgsfield_video`、`grok_video`、`gemini_omni_video`、`heygen_video`、`jimeng_video`、`minimax_video / minimax_video_direct` | 高 | key 管理 + HMAC-SHA256 V4 签名（如 jimeng） |

**设计选项**：
- **A**：全部经 BFF 代理（key 留服务端），客户端走 MCP/BFF 协议 → 简单安全
- **B**：用户自带 key，客户端直连 → 释放服务端配额压力，复杂度上升
- **推荐**：混合模式——中转站类（`kling_relay`、`seedance_relay`）天然适合 B；付费 SDK 类（OpenAI/Google）走 A

---

## 8. 推荐架构

```
┌──────────────────────────────────────────────────────────────────┐
│                    服务端 (OpenMontage)                            │
│  角色: 编排 / 调度 / 决策 / GPU 池 / API key 管理 / 发布          │
├──────────────────────────────────────────────────────────────────┤
│ • 元编排层:  pipeline manifests + stage directors + meta skills   │
│ • 选择器:     tts_selector / video_selector / image_selector      │
│ • 重 GPU 池:  Remotion / HyperFrames / LTX-22B / Hunyuan /       │
│              Wan-14B / FLUX / SD / Wav2Lip / SadTalker / LLaVA   │
│ • 重 API 代理:  付费 TTS / 视频生成 / 音乐生成（统一 key 管理）    │
│ • MCP/BFF:    asset_upload / chunk / read_session_asset /         │
│              rsync_upload                                         │
│ • 数学动画:   math_animate（沙箱）                                │
│ • 决策/审计:  decision_log / checkpoint / cost_tracker            │
└──────────────────────────────────────────────────────────────────┘
                            ▲ ▼ (编排脚本 JSON / MCP 协议)
┌──────────────────────────────────────────────────────────────────┐
│                    客户端 (专用)                                   │
│  角色: 原始媒体处理 / 本地模型推理 / 隐私敏感操作                  │
├──────────────────────────────────────────────────────────────────┤
│ • 媒体采集:   screen_recorder / cap_recorder / video_downloader   │
│ • 本地音频:   piper_tts / kokoro_tts / funasr_transcriber /       │
│              transcriber(tiny–small) / argos_translator /         │
│              music_gen_local(small) / audio_mixer / audio_enhance │
│ • 轻量渲染:   FFmpeg.wasm (silence_cutter / stitch / auto_reframe│
│              / caption_burn) / WebCodecs                           │
│ • 分析预处理: frame_sampler / scene_detect / face_tracker /        │
│              visual_qa / audio_energy / audio_probe (只回传结果)   │
│ • 图像增强:   bg_remove (ONNX.wasm) / eye_enhance (MediaPipe)    │
│ • 字符动画:   character_animation 的 5 个 JSON 工具               │
│ • Stock/Graph: pexels/pixabay(API 直连) / code_snippet /          │
│              diagram_gen / image_selector 评分                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. 关键设计原则

1. **`runtime` 字段就是切分金标准**：`base_tool.py` 已用 `ToolRuntime.{LOCAL, LOCAL_GPU, API, HYBRID}` 标注。客户端 SDK 只需按 `runtime == LOCAL` 过滤即可加载。
2. **`resource_profile` 是路由信号**：服务端调度器读取 `vram_mb` 和 `cpu_cores`，在"服务端 GPU 池 / 客户端本地 / BFF 代理 API"三者之间路由。
3. **selector 双层化**：`tts_selector`/`video_selector`/`image_selector` 留在服务端做"编排决策"；具体的 LOCAL provider 工具暴露给客户端调用。这样**编排逻辑集中，重负载分散**。
4. **服务端不再是"重负载执行者"，而是"决策 + 重 GPU + 协议入口"**：
   - 重 GPU：留给 5–6 个真正吃满 H100/A100 的工具
   - 重 API key 代理：避免 key 下发到客户端
   - 重协议：MCP/BFF 入口维持现状
5. **客户端"原始媒体零上传"原则**：`frame_sampler / scene_detect / transcriber / visual_qa` 客户端执行后只回传 KB 级 JSON 与关键帧，省 95% 带宽。
6. **隐私敏感数据留在客户端**：语音（TTS 输入文本 / STT 音频）、屏幕录制、视频源文件——全部不出客户端。

---

## 10. 迁移优先级（按 ROI）

| 优先级 | 类别 | 工具数 | 释放资源 | 实施成本 |
|---|---|---|---|---|
| **P0** | 屏幕录制 + 视频下载 + 本地 TTS/STT 轻量模型 | ~7 | 大量 CPU + 带宽 | 低（结构简单） |
| **P1** | FFmpeg-only 工具下沉（silence_cutter/stitch/caption_burn/auto_reframe） | ~5 | CPU 长跑占用 | 中（需 FFmpeg.wasm 集成） |
| **P2** | 本地音频模型下沉（kokoro/piper/funasr/music_gen_local/argos） | ~6 | 释放服务端模型 + 推理 | 低（模型随包走） |
| **P3** | 视频分析预处理下沉（frame_sampler/scene_detect/face_tracker/visual_qa） | ~5 | **节省 95% 上行带宽** | 低（FFmpeg + MediaPipe） |
| **P4** | 16 个 API 视频工具可选下沉（经 BFF 代理或用户自带 key） | 16 | 释放服务端代理开销 | 中（key 管理） |
| **P5** | 图像增强下沉（bg_remove / eye_enhance） | 2 | 释放 GPU/带宽 | 中（需 ONNX.wasm） |
| **保留** | Remotion / HyperFrames / LTX-22B / Hunyuan / FLUX / LLaVA / Wav2Lip / math_animate | ~8 | — | — |

---

## 11. 涉及的工具清单（绝对路径速查）

### 视频渲染（11 个）
- `tools/video/video_compose.py` — 三引擎编排（FFmpeg/Remotion/HyperFrames）
- `tools/video/hyperframes_compose.py`
- `tools/video/video_stitch.py`
- `tools/video/remotion_caption_burn.py`
- `tools/video/auto_reframe.py`
- `tools/video/green_screen_processor.py`
- `tools/video/silence_cutter.py`
- `remotion-composer/` — React 渲染层

### AI 视频生成（22 个）
- LOCAL_GPU：`cogvideo_video.py`、`comfyui_video.py`、`hunyuan_video.py`、`ltx_video_local.py`、`ltx_video_modal.py`、`wan_video.py`
- API：`gemini_omni_video.py`、`grok_video.py`、`heygen_video.py`、`higgsfield_video.py`、`jimeng_video.py`、`kling_official_video.py`、`kling_relay.py`、`kling_video.py`、`minimax_video.py`、`minimax_video_direct.py`、`runway_video.py`、`seedance_video.py`、`seedance_replicate.py`、`seedance_relay.py`、`sora_video.py`、`veo_video.py`

### 图像与图形（17 个）
- `tools/graphics/` 下全部
- `tools/character/character_animation.py`（6 个子工具）
- `tools/enhancement/` 下全部

### 音频（20+ 个）
- `tools/audio/` 下 TTS（10 个）+ 音乐（6 个）+ 混音/增强（2 个）
- `tools/subtitle/subtitle_gen.py`
- `tools/translation/`（Argos/NLLB）
- `tools/analysis/transcriber.py`、`funasr_transcriber.py`、`azure_stt.py`、`dashscope_asr.py`

### 分析与捕获（13 个）
- `tools/analysis/` 下：audio_energy/audio_probe/composition_validator/face_tracker/frame_sampler/scene_detect/transcriber/video_analyzer/video_understand/video_downloader/visual_qa
- `tools/capture/` 下：cap_recorder/screen_recorder/screen_capture_selector
- `tools/avatar/` 下：kling_avatar/kling_lip_sync/lip_sync/talking_head

### 资产上传（6 个，必须服务端）
- `tools/asset_upload.py`、`asset_upload_chunk.py`、`tools/asset/read_session_asset.py`、`read_session_asset_image.py`、`tools/rsync_upload.py`

---

## 12. 下一步建议

1. **建立客户端 SDK 入口**：客户端只暴露 `runtime == LOCAL` 的工具，按 `name` 自动发现；服务端继续暴露所有 selector。
2. **引入 `client_capable` 字段**：在 `BaseTool` 加布尔字段，标记该工具是否可下放（默认 False，LOCAL 工具设为 True）。
3. **BFF 代理层**：对 16 个 API 视频工具统一 key 注入，客户端走 BFF HTTP 调用即可。
4. **Backlot 实时上屏**：客户端执行后通过 `events.jsonl` + `rsync_upload` 回传结果，Backlot 状态对用户透明。
5. **失败降级策略**：客户端工具执行失败 → 服务端兜底重试 + `decision_log` 记录变更。

详见分项详细分析：
- [`video-render-load-analysis-2026-09-01.md`](video-render-load-analysis-2026-09-01.md)
- [`ai-video-gen-load-analysis-2026-09-01.md`](ai-video-gen-load-analysis-2026-09-01.md)
- [`image-graphics-load-analysis-2026-09-01.md`](image-graphics-load-analysis-2026-09-01.md)
- [`audio-load-analysis-2026-09-01.md`](audio-load-analysis-2026-09-01.md)
- [`analysis-capture-load-analysis-2026-09-01.md`](analysis-capture-load-analysis-2026-09-01.md)
