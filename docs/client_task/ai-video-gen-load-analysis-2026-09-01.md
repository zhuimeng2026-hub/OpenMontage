# AI 视频生成工具负载分析（详细）

**日期**：2026-09-01
**范围**：`tools/video/` 下 22 个 AI 视频生成工具

---

## 总览表

| # | 工具 | runtime | GPU/API | 环境变量 (API key) | 输入大小 | 输出大小 | 可离线 | 单次耗时 | 客户端友好度 |
|---|------|---------|---------|--------------------|----------|----------|--------|----------|--------------|
| 1 | `cogvideo_video` | LOCAL_GPU | 本地 GPU 推理 | (无 key, 下载 HF 模型) | prompt KB / ref图 MB | 单视频 5–20 MB | 是 | 分钟级 (CogVideoX-5B, VRAM 12 GB) | **中** — 5B 模型 12 GB VRAM，Blackwell/DGX 客户端可跑 |
| 2 | `comfyui_video` | LOCAL_GPU | 本地 ComfyUI 服务 | (无 key) | prompt KB / ref图 MB | 单视频 5–30 MB | 是 | 3.5–4 分钟 (WAN 2.2 14B FP8 + 4-step LightX2V LoRA, VRAM 16 GB) | **低** — 工作流/模型 14B 参数, "真"重负载 |
| 3 | `gemini_omni_video` | API | 远程 | `GEMINI_API_KEY` / `GOOGLE_API_KEY` | prompt KB / ref图 MB / 视频 MB | 单视频 10–30 MB | 否 | ~3 分钟 (180 s) | **高** — 完全 API 化 |
| 4 | `grok_video` | API | 远程 (xAI) | `XAI_API_KEY` | prompt KB / ref图 MB | 单视频 10–40 MB | 否 | ~90 s + 8 s/秒视频 | **高** — 只调一次 API + 轮询 |
| 5 | `heygen_video` | API | 远程 (HeyGen gateway) | `HEYGEN_API_KEY` | prompt KB / ref图 MB | 单视频 10–40 MB | 否 | 30–300 s 视 provider | **高** — 单一聚合网关 |
| 6 | `higgsfield_video` | API | 远程 (Higgsfield Cloud) | `HIGGSFIELD_API_KEY` + `HIGGSFIELD_API_SECRET` | prompt KB / ref图 MB | 单视频 10–30 MB | 否 | 60–120 s | **高** — 单一 Bearer token |
| 7 | `hunyuan_video` | LOCAL_GPU | 本地 GPU | (无 key) | prompt KB / ref图 MB | 单视频 5–20 MB | 是 | 分钟级 (Hunyuan-1.5, VRAM 14 GB) | **中** — 14 GB VRAM, 客户端独显可跑 |
| 8 | `jimeng_video` | API | 远程 (Volcengine) | `VOLC_ACCESSKEY` + `VOLC_SECRETKEY` | prompt KB / ref图 URL | 单视频 5–30 MB | 否 | 2–4 分钟 | **高** — HMAC-SHA256 V4 客户端实现可复用 |
| 9 | `kling_official_video` | API | 远程 (Kling 官方) | `KLING_API_KEY` | prompt KB / 多 ref图 / video URL | 单视频 10–40 MB | 否 | ~3 分钟 | **高** — submit/poll/download |
| 10 | `kling_video` | API | 远程 (fal.ai 代理) | `FAL_KEY` | prompt KB / ref图 URL | 单视频 10–40 MB | 否 | ~60 s | **高** — 极薄 wrapper |
| 11 | `kling_relay` | API | 远程 (new-api 中转站) | `VIDEO_RELAY_BASE_URL` + `VIDEO_RELAY_API_KEY` | prompt KB / ref图 URL | 单视频 10–40 MB | 否 | ~60 s | **高** — 中转站模式天然适合客户端 |
| 12 | `ltx_video_local` | LOCAL_GPU | 本地 GPU (LTX-2 22B) | (无 key) | prompt KB / ref图 MB | 单视频 10–40 MB | 是 | **重负载** — VRAM 12 GB+ | **低** — 22B 模型, 客户端除非 H100/A100/4090 |
| 13 | `ltx_video_modal` | API | 自托管 Modal GPU | `MODAL_LTX2_ENDPOINT_URL` | prompt KB / ref图 MB | 单视频 10–40 MB | 否 | ~3 分钟 | **中** — 客户端只是 HTTP 客户端 |
| 14 | `minimax_video` | API | 远程 (fal.ai 代理) | `FAL_KEY` | prompt KB / ref图 URL | 单视频 10–30 MB | 否 | 30–60 s | **高** — 极薄 wrapper |
| 15 | `minimax_video_direct` | API | 远程 (MiniMax 直连) | `MINIMAX_API_KEY` | prompt KB / ref图 URL | 单视频 10–40 MB | 否 | 1.5–5 分钟 | **高** — 三步 HTTP |
| 16 | `runway_video` | API | 远程 (Runway) | `RUNWAY_API_KEY` | prompt KB / ref图 URL | 单视频 10–40 MB | 否 | 25–120 s | **高** — 纯 HTTP 客户端 |
| 17 | `seedance_video` | API | 远程 (fal.ai) | `FAL_KEY` | prompt KB / 多 ref图/视频/音频 URL | 单视频 10–50 MB | 否 | 60–120 s | **高** — 纯 HTTP + 轮询 |
| 18 | `seedance_replicate` | API | 远程 (Replicate) | `REPLICATE_API_TOKEN` | prompt KB / ref图 URL | 单视频 10–50 MB | 否 | 60–120 s | **高** — 纯 HTTP 客户端 |
| 19 | `seedance_relay` | API | 远程 (new-api 中转站) | `VIDEO_RELAY_BASE_URL` + `VIDEO_RELAY_API_KEY` | prompt KB / ref图 URL | 单视频 10–50 MB | 否 | ~120 s | **高** — 中转模式客户端最干净 |
| 20 | `sora_video` | API | 远程 (OpenAI SDK) | `OPENAI_API_KEY` | prompt KB / ref图 MB | 单视频 10–40 MB | 否 | 30 s–6 分钟 | **高** — OpenAI 官方 SDK |
| 21 | `veo_video` | API | 远程 (Google Veo 3.1 或 fal.ai) | `GEMINI_API_KEY` / `FAL_KEY` | prompt KB / ref图 MB / 首末帧 | 单视频 20–80 MB (1080p/4K) | 否 | 45–120 s | **高** — 纯 HTTP/SDK 调用 |
| 22 | `wan_video` | LOCAL_GPU | 本地 GPU | (无 key) | prompt KB / ref图 MB | 单视频 5–20 MB | 是 | 分钟级 (Wan 2.1 1.3B–14B) | **中** — 1.3B 8GB 可客户端; 14B 应服务端 |

---

## 真正的"重负载"本地 GPU 路径（不应从服务端剥离）

| 工具 | 模型 | VRAM 需求 | 单次耗时 | 推荐保留原因 |
|------|------|-----------|----------|--------------|
| **`ltx_video_local`** | LTX-2 22B | 12 GB+ (保守 24 GB) | 4 分钟级 / 121 帧 30 fps | 22B 参数模型, GPU-bound heavy compute |
| **`comfyui_video`** | WAN 2.2 14B ×2 + UMT5-XXL + 双 LoRA + VAE | 16 GB+ bundled | T2V 240 s, I2V 210 s | 工作流依赖一整套模型权重 |
| `ltx_video_modal` | LTX-2 22B (Modal) | 24 GB+ | ~3 分钟 | Modal 后端仍是 22B GPU 重负载 |
| `wan_video` (14B variant) | Wan 2.1 14B | 14 GB+ | 分钟级 | 14B 应留在服务端 |
| `hunyuan_video` | Hunyuan-1.5 | 14 GB VRAM | 分钟级 | 14 GB 是客户端独显上限 |
| `cogvideo_video` (5B) | CogVideoX 5B | 12 GB VRAM | 分钟级 | 接近客户端独显上限 |

---

## 客户端友好度评级

### 高（应剥离）— 16 个 API 类工具

**全部 16 个 API 工具资源 profile 一致**（CPU 1 / RAM 512MB / VRAM 0 / 磁盘 500MB / 网络必填），瓶颈仅在网络而非计算：

- `gemini_omni_video`, `grok_video`, `heygen_video`, `higgsfield_video`, `jimeng_video`
- `kling_official_video`, `kling_video`, `kling_relay`
- `minimax_video`, `minimax_video_direct`
- `runway_video`, `seedance_video`, `seedance_replicate`, `seedance_relay`
- `sora_video`, `veo_video`

**例外**：`sora_video` 需 `openai>=2.44.0` SDK，客户端打包需注意；`jimeng_video` 需 HMAC-SHA256 V4 签名逻辑。

### 中（视场景决定）

- `wan_video` (1.3B variant 客户端 8GB / 14B variant 服务端)
- `cogvideo_video` (2B 8GB 客户端 / 5B 12GB 服务端)
- `hunyuan_video` (14GB 是客户端独显上限)
- `ltx_video_modal` (已是 HTTP API, 客户端触发器)

### 低（重负载，必须保留服务端）

- `comfyui_video` (14B 工作流)
- `ltx_video_local` (22B 模型)

---

## 关键结论

1. **16 个 API 工具可全部客户端化**——输入 KB 级 prompt + MB 级 ref 图，输出 MB 级单视频，瓶颈仅在网络。
2. **真正的重负载仅 4–6 个**：`comfyui_video`、`ltx_video_local`、`ltx_video_modal`、`wan_video` 14B、`hunyuan-1.5`、`cogvideo-5b`。
3. **优先级剥离建议**：先剥离 `seedance_video`/`runway_video`/`sora_video`/`veo_video`（单一 API key + 标准 SDK，客户端 SDK 已成熟）；中转站类（`kling_relay`、`seedance_relay`）天然适合客户端。
4. **服务端应保留**：所有 LOCAL_GPU 路径（按设备能力路由）、`ltx_video_modal` 作为私有云 GPU 兜底、`comfyui_video` 作为自定义工作流入口。
