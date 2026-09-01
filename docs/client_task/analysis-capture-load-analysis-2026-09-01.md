# 分析与捕获工具负载分析（详细）

**日期**：2026-09-01
**范围**：`tools/analysis/`（11 个）、`tools/capture/`（3 个）、`tools/avatar/`（4 个）、`tools/asset/`（2 个）、`tools/asset_upload.py` / `asset_upload_chunk.py` / `tools/rsync_upload.py`

---

## 1. `tools/analysis/`

| 工具 | runtime | 主要消耗点 | 重型模型 | 输入量级 | 输出量级 | 外部硬件 | 单次耗时 | 客户端友好度 | 定位 |
|---|---|---|---|---|---|---|---|---|---|
| `audio_energy.py` | `local` | CPU:ebur128 / RAM:128MB | 否（ffmpeg ebur128） | mp3/wav（KB–MB） | 1s粒度 loudness JSON | 否 | 数秒~数十秒 | **高** | 客户端可独立运行 |
| `audio_probe.py` | `local` | CPU:ffprobe / RAM:64MB | 否 | 任意音视频 | 几行 metadata | 否 | <1s | **高** | 最轻量探针 |
| `composition_validator.py` | `local` | CPU:JSON 校验 / RAM:64MB | 否（ffprobe 子调用） | composition JSON（KB） | errors/warnings/info 列表 | 否 | <0.5s | **高** | 纯静态检查 |
| `face_tracker.py` | `local` | CPU:MediaPipe/OpenCV / RAM:1GB | **是**（MediaPipe Face Mesh / OpenCV Haar） | 视频（几百 MB） | faces JSON | 否（CPU 可跑）| 30s 视频 ~5–15s | **中** | 客户端可做 |
| `frame_sampler.py` | `local` | CPU:ffmpeg + Pillow / RAM:512MB | 否 | 视频（几十 MB–几 GB） | 帧图（jpg/png） | 否 | 时长 × 0.1–0.3 | **高** | 客户端可独立跑 |
| `scene_detect.py` | `local` | CPU:PySceneDetect / RAM:1GB | 是（PySceneDetect, OpenCV） | 视频 | scene list JSON | 否 | 与时长线性 | **中** | 长视频慢但可跑 |
| `transcriber.py` | `local` | CPU:faster-whisper / RAM:2GB | **是**（faster-whisper base–large-v3） | 音视频 | transcript JSON | 否但 GPU 推荐 | 时长 × 0.3–0.5（CPU base） | **中–高** | **客户端化收益最大** |
| `video_analyzer.py` | `local` | 编排器：递归调用子工具 / RAM:2GB | 是（Whisper+PySceneDetect+OpenCV） | URL 或本地视频 | VideoAnalysisBrief | 否（GPU 加速子工具时受益） | 2–10 分钟 | **中** | 整体建议客户端运行 |
| `video_understand.py` | `local_gpu` | GPU:CLIP/BLIP-2/LLaVA / RAM:4GB / VRAM:2GB+ | **是**（CLIP-ViT-B/32、BLIP-2 2.7B、LLaVA-1.5 7B） | 图像/视频（≤几 GB） | captions/QA/scene JSON | **是** — 必须 GPU | 1 帧 ~0.5–5s | **低** | **保持服务端** |
| `video_downloader.py` | `local` | CPU+网络 / RAM:512MB | 否（yt-dlp） | URL | 视频/音频/字幕 | 否 | 720p 10–60s | **中** | **客户端优先** |
| `visual_qa.py` | `local` | CPU:ffmpeg/ffprobe / RAM:512MB | 否 | 视频 + 时间戳 | review 帧 + audio_levels | 否 | 数秒 | **高** | 客户端预览抽帧 |

## 2. `tools/capture/`

| 工具 | runtime | 主要消耗点 | 重型模型 | 输入量级 | 输出量级 | 外部硬件 | 单次耗时 | 客户端友好度 | 定位 |
|---|---|---|---|---|---|---|---|---|---|
| `cap_recorder.py` | `local` | CPU+磁盘 IO / RAM:64MB | 否（Cap 应用已含 GPU 编码） | 用户在 Cap UI 中录 | 已有 MP4 路径 | **是** — Cap 桌面 GUI | 用户操作 30s–10min | **必须客户端** | **强制客户端** |
| `screen_recorder.py` | `local` | CPU:libx264 / RAM:512MB | 否（仅 ffmpeg） | 录屏参数 | MP4 | **是** — 必须有真实屏幕 | 等于 duration（上限 600s） | **必须客户端** | **强制客户端** |
| `screen_capture_selector.py` | `hybrid` | CPU 调度 / RAM:64MB | 否（只路由） | 操作类型 | 推荐结果或子工具结果 | 通过子工具继承 | <1s + 子工具耗时 | **必须客户端** | **强制客户端** |

## 3. `tools/avatar/`

| 工具 | runtime | 主要消耗点 | 重型模型 | 输入量级 | 输出量级 | 外部硬件 | 单次耗时 | 客户端友好度 | 定位 |
|---|---|---|---|---|---|---|---|---|---|
| `kling_avatar.py` | `api` | CPU:HTTP / RAM:512MB | 否（Kling 云端） | portrait + 音频 | MP4 | 否（云渲染）| p50 ~240s | **低** — 已为 API | 客户端直连即可 |
| `kling_lip_sync.py` | `api` | CPU:HTTP / RAM:512MB | 否 | 视频 URL + 音频 | MP4 | 否 | identify ~15s；lip_sync ~240s | **低** — 同上 | 客户端直连 |
| `lip_sync.py` | `local_gpu` | CPU+GPU（Wav2Lip）/ RAM:4GB / VRAM:4GB+ | **是**（Wav2Lip / Wav2Lip-GAN） | 视频+音频 | MP4 | **是** — 必须 CUDA | 数分钟 | **低** — 客户端需 CUDA | 客户端可选，否则保持服务端 |
| `talking_head.py` | `local_gpu` | CPU+GPU（SadTalker）/ RAM:4GB / VRAM:4GB+ | **是**（SadTalker / MuseTalk） | 静态人像+音频 | MP4 | **是** — 必须 CUDA | 30–120s | **低** — 同上 | 同上 |

## 4. `tools/asset/` + 上传（必须服务端）

| 工具 | runtime | 主要消耗 | 客户端友好度 | 定位 |
|---|---|---|---|---|
| `read_session_asset_image.py` | `local` | CPU:读 bytes / RAM:128MB | **N/A** — MCP ImageContent 出口 | **必须服务端**（FastMCP 出口） |
| `read_session_asset.py` | `local` | CPU:读 bytes+base64 / RAM:128MB | **N/A** | **必须服务端**（BFF 用） |
| `asset_upload.py` | `local` | CPU:base64 解码+hash / RAM:256MB / 磁盘:100MB | **N/A** — MCP 上传入口 | **必须服务端**（中心化项目工作区） |
| `asset_upload_chunk.py` | `local` | CPU:hash+落盘 / RAM:256MB / 磁盘:100MB | **N/A** | **必须服务端** |
| `rsync_upload.py` | `local` | CPU+网络 / RAM:128MB | **N/A** — 发布出口 | **服务端/发布端** |

---

## 关键标注

### 2.1 必须客户端执行的"屏幕录制 / 上传"型

| 工具 | 不可下放服务端的原因 |
|---|---|
| `screen_recorder.py` | 拉取**本地桌面会话**的 framebuffer，离开客户端无意义 |
| `cap_recorder.py` | 桥接**桌面端 Cap 应用**；output 路径来自 `AppData`/`Library/Application Support` |
| `screen_capture_selector.py` | 调度器，路由到上述两者 |

### 2.2 强烈建议剥离到客户端

| 工具 | 理由 |
|---|---|
| `video_downloader.py` | 服务器下行带宽贵；yt-dlp 客户端零成本；规避 YouTube"非服务端下载"策略 |
| `transcriber.py` | **语音是隐私敏感数据**；客户端 tiny/base 模型既快又不出网；服务端只接收文本 |
| `frame_sampler.py`、`visual_qa.py`、`audio_energy.py`、`audio_probe.py`、`composition_validator.py` | 都是 ffmpeg/JSON 校验，CPU/IO 低，完全可预跑客户端，**只把"判定结果"或"关键帧"上传**，节省 95% 带宽 |
| `scene_detect.py` | 与 transcriber 串联：客户端先粗筛场景，再决定要上传哪些切片 |

### 2.3 维持服务端（GPU/超大模型）

| 工具 | 维持服务端的理由 |
|---|---|
| `video_understand.py`（CLIP/BLIP-2/LLaVA） | 7B 模型 + 2GB+ VRAM，普通笔记本 30s/帧 |
| `lip_sync.py`（Wav2Lip）、`talking_head.py`（SadTalker） | 需要 CUDA + 4GB VRAM |
| `kling_avatar.py`、`kling_lip_sync.py` | 已是云 API；客户端直连即可，服务端只是代理 |

### 2.4 维持服务端的 MCP/BFF 入口

`asset_upload.py`、`asset_upload_chunk.py`、`read_session_asset.py`、`read_session_asset_image.py`、`rsync_upload.py`

---

## 简要结论

1. **客户端化收益最大的 3 个**：① `transcriber.py`（隐私+带宽），② `video_downloader.py`（节省出口带宽），③ `screen_recorder.py` + `cap_recorder.py` + `screen_capture_selector.py`（架构上不可能服务端跑）。
2. **应当服务端保留**：所有需要 GPU/CUDA 的视觉/口型/数字人（`video_understand`/`lip_sync`/`talking_head`），以及所有 MCP/BFF/发布入口类。
3. **视频分析 orchestrator（`video_analyzer.py`）应被重新设计**：把"短视 transcript"放到客户端，"deep analysis"留给服务端；它已经把依赖拆成了子工具，迁移成本最低。
4. **整体策略**：把 `analysis/*` 中除 `video_understand` 外的工具和 `download/transcribe/screen-capture` 系列都打包进"客户端边车"（CLI/JS SDK），让"原始媒体"留在客户端，"分析结果/关键帧/转写文本"回服务端流水线；服务端只跑 GPU 重活和发布。
