# 视频渲染工具负载分析（详细）

**日期**：2026-09-01
**范围**：video 渲染相关 11 个工具 + remotion-composer React 渲染层

---

## 工具负载与可执行性对比表

| 工具 | runtime | 依赖核心 | CPU/GPU/内存/磁盘/网络 | 输入/输出大小量级 | 离线/客户端运行 | 重型模型/GPU | 单次耗时量级 | 客户端执行判断 |
|---|---|---|---|---|---|---|---|---|
| **video_compose.py** (FFmpeg 路径) | `LOCAL` | ffmpeg + ffprobe | CPU 4 核 / 无 GPU / 2GB RAM / 5GB 磁盘 / 不联网 | 输入 KB-GB 视频片段，输出 100MB-数 GB MP4 | 完全可离线 | 否 | 数十秒~数分钟 | **适合**（FFmpeg.wasm / WebCodecs）|
| **video_compose.py** (Remotion 路径) | `LOCAL` | Node ≥22 + npx + `remotion-composer/` node_modules (~870MB) + headless Chrome + ffmpeg | CPU 多核 / 无 GPU / **0.5–1GB+ RAM 每渲染实例** / 5GB+磁盘 / CDN 加载 GSAP & 字体 | 输入素材 KB-GB + 巨大 props JSON | 可离线（已下载 npm 包）；**浏览器无法跑 Node** | 否（但需要 Chrome）| 数分钟~十数分钟 | **不适合**（需 Node + bundler + Chrome 子进程）|
| **video_compose.py** (HyperFrames 路径) | `LOCAL` | Node ≥22 + npx + `npx hyperframes`（首次下载）+ ffmpeg | 同上 | workspace HTML+assets，输出 MP4 | 可离线 | 否 | 同上 | **不适合**（依赖 Node 生态）|
| **hyperframes_compose.py** | `ToolRuntime.LOCAL` | Node ≥22 + npx + npm 包 `hyperframes` + ffmpeg | CPU 多核 / 无 GPU / 3GB RAM / 2GB 磁盘 / npm view 联网探测 | 整个 workspace（含 HTML/CSS/JS+素材） | 本地可运行（npm 包就绪后） | 否 | 单次 30min timeout；estimate ~0.5x实时 | **不适合**（必须 Node 22+ + 全套 npm 工具链 + 浏览器校验）|
| **video_stitch.py** | `LOCAL` | ffmpeg + ffprobe | CPU 4 核 / 无 GPU / 2GB RAM / 5GB 磁盘 / 不联网 | 多段 MP4 各 100MB-数 GB，输出 GB 级 | 完全可离线 | 否 | 数十秒~数分钟（cut 快、xfade链 N 段慢）| **适合**（纯 FFmpeg，浏览器侧 FFmpeg.wasm/WebCodecs 可承担；xfade 复杂链是大宗负载但可接受）|
| **remotion_caption_burn.py** (Remotion 路径) | `LOCAL` | npx + remotion-composer + headless Chrome + ffmpeg | 同 Remotion 路径 | 输入 MP4 + word-level segments/srt（KB）+ 拷贝输入视频到 public/ | 可离线（Remotion 装好后）| 否 | 数分钟（输入视频被复制+重渲染整片）| **不适合**（需 Node + Chrome）|
| **remotion_caption_burn.py** (FFmpeg 回退路径) | `LOCAL` | ffmpeg | CPU 4 核 / 无 GPU / 2GB RAM / 0.5GB 磁盘 / 不联网 | 同上 | 完全可离线 | 否 | 数十秒~1 分钟 | **适合**（纯 FFmpeg，烧字幕是经典客户端能力）|
| **remotion-composer/** | n/a（React 渲染层）| Node + Remotion 4.0.508 + React18 + headless Chrome + 30 woff2 字体 | CPU 多核 / 无 GPU / bundler + Chrome ~1GB+ / node_modules 870MB / GSAP + 字体从 CDN 加载 | 由调用方传入 props JSON | npm 包安装后可离线，但本身是服务器侧产物 | 否 | 与父工具耗时绑定 | **不适合客户端承载**（870MB 依赖 + Node运行时）|
| **auto_reframe.py** | `LOCAL` | ffmpeg + ffprobe + 可选 MediaPipe/OpenCV 面部追踪 | CPU 4 核 / 无 GPU / 2GB RAM / 4GB 磁盘 / 不联网 | 输入 MP4 (100MB-GB)，输出 MP4 | 完全可离线；MediaPipe 有 WASM 版本 | 否（CV 仅 CPU）| ~1x 实时（默认估算 60s）| **适合**（MediaPipe 可在浏览器 WASM 跑；sendcmd 动态裁切需特判但仍可移植）|
| **green_screen_processor.py** (chromakey) | `LOCAL` | ffmpeg + ffprobe | CPU 4 核 / 无 GPU / 4GB RAM / **8GB 磁盘（逐帧 PNG）** / 不联网 | 30s 视频 @15fps = 450 帧 PNG（每帧200KB-2MB）| 完全可离线 | 否 | ~30s（按 estimate_runtime）| **适合**（逐帧 ffmpeg 命令，理论可移植，但海量小命令开销大）|
| **green_screen_processor.py** (rembg AI) | `LOCAL` | rembg + u2net_human_seg + onnxruntime + Pillow + numpy | CPU 4 核 / **GPU 加速（rembg[gpu]）** / 4GB+ RAM / 8GB 磁盘 / 不联网 | 同上 | 可离线；模型需下载一次 | **是（u2net 175MB 模型，理想要 GPU）**| ~120s（estimate）/ 数分钟-GPU /半小时-CPU | **适合有 GPU 的客户端**（onnxruntime-web 可在浏览器 WASM/WebGL 跑 u2net）；**纯 CPU 客户端不建议** |
| **silence_cutter.py** | `LOCAL` | ffmpeg + ffprobe | CPU 4 核 / 无 GPU / 2GB RAM / 4GB 磁盘 / 不联网 | 输入 MP4，输出 cut 后 MP4 | 完全可离线 | 否 | ~45s（estimate）| **适合**（纯 FFmpeg，silencedetect 是经典客户端能力）|

---

## 关键负载观察

- **最大单点瓶颈**：`video_compose.py` 的 Remotion 路径。每次渲染启动一个 bundler + headless Chrome 子进程，耗0.5–1GB+ 内存（已在 `_get_remotion_max_parallel = 2` 注释中说明原因）。这是当前最不适合客户端承载的环节。
- **Remotion node_modules 总量870MB**，外加 CDN 拉 GSAP（`gsap@3.14.2`）和 30 个 woff2 字体（冷启动 30 次 FontFace 网络往返）。即便在浏览器侧用 esbuild bundler，也无法直接复用这套 React/Remotion 4渲染栈。
- **HyperFrames 同样依赖 Node ≥22 + npx + npm 包**，首次使用触发 `npm view hyperframes version` 联网探测（5s timeout），然后 `npx hyperframes` 临时下载 CLI；浏览器侧没有等价的 npx 机制。
- **绿色处理 chromakey 路径**看似纯 FFmpeg 友好，但实现是"对每帧独立启动一次 ffmpeg"（逐帧 PNG 中转），400+ 子进程开销对浏览器 WebAssembly FFmpeg 极不友好；应改为单次 filtergraph 调用才能在客户端落地。
- **face-tracked reframe** 用 FFmpeg `sendcmd` filter 写一个时间序列命令文件，浏览器侧 FFmpeg.wasm 也支持，但当前实现是 sendcmd + 兜底静态裁切，移植时需注意。
- **silence_cutter / video_stitch** 是最干净的纯 FFmpeg 任务，零网络、零 GPU、无重型模型，移植到客户端的工程阻力最小。

---

## 客户端迁移优先级

1. **强烈推荐剥离到客户端**：`silence_cutter.py`、`video_stitch.py`（cut/concat/normalize路径）、`auto_reframe.py`（静态裁切）、`remotion_caption_burn.py` 的 FFmpeg 回退路径、`video_compose.py` 的 FFmpeg-only `compose`/`encode`/`burn_subtitles`/`overlay` 操作。
2. **可剥离但需重写**： `green_screen_processor.py` 的 chromakey 路径——把"逐帧启动 ffmpeg"改成单 filtergraph 调用；rembg 路径——只对有 GPU 的客户端开放。
3. **保留在服务端**：`remotion-composer/` 整个 React/Remotion 栈、`hyperframes_compose.py`、`video_compose.py` 的 `remotion_render` / `remotion_bilingual_overlay` / `render`（当 render_runtime==remotion 或 hyperframes 时）。这些都需要 Node + npm + headless Chrome，浏览器侧没有原生承载方式。
