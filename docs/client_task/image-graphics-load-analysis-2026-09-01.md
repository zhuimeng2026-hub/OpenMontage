# 图像生成 / 字符动画 / SVG 渲染负载分析（详细）

**日期**：2026-09-01
**范围**：`tools/graphics/`（17 个）、`tools/character/character_animation.py`（6 个子工具）、`tools/enhancement/`（6 个）

---

## A. `tools/graphics/`（图像生成/渲染）

| 工具 | Runtime | CPU/GPU/内存/磁盘 | 依赖模型 | I/O 量级 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `comfyui_image` | **LOCAL_GPU** | 2C / 8GB VRAM / 8GB RAM / 500MB | FLUX 2 Dev NVFP4 + Mistral TE + FLUX VAE（3 个 .safetensors） | prompt → 1024² PNG | ~30s | **低** — Blackwell/DGX 必需 |
| `local_diffusion` | **LOCAL_GPU** | 2C / 4GB VRAM / 8GB RAM / 5GB | SD 2.1-base（首次下载 ~5GB） | prompt → 512² PNG | ~30s/张（GPU） | **低** — SD 权重 + VRAM |
| `image_gen` (deprecated) | **HYBRID** | 1C / 0 VRAM / 512MB / 100MB | 调用子工具 | 同子工具 | 同子工具 | **中** — 仅路由 |
| `image_selector` | **HYBRID** | 1C / 0 VRAM / 512MB / 100MB | 无（仅打分路由） | prompt → 评分 | <100ms | **高** — 纯逻辑编排 |
| `flux_image` | **API** | 1C / 0 / 512MB / 100MB | FLUX-pro/dev（fal.ai） | prompt → 1024² PNG | 5–15s | **中** — 远端 API |
| `dashscope_image` | **API** | 1C / 0 / 512MB / 100MB | Qwen-Image-2.0 / Wan | prompt → ≤2048² | 5–20s | **中** — 阿里云 API |
| `google_imagen` | **API** | 1C / 0 / 512MB / 100MB | Imagen-4.0 | prompt → ≤1344² PNG | 5–15s | **中** — Google API |
| `grok_image` | **API** | 1C / 0 / 512MB / 100MB | grok-imagine-image（xAI） | prompt + 可选多图 → ≤2K | 5–20s | **中** — xAI API |
| `kling_official_image` | **API** | 1C / 0 / 512MB / 200MB | Kling v3 / Omni Image | prompt + 多参考图 → ≤4K PNG | ~90s | **中** — 官方 Kling API |
| `minimax_image` | **API** | 1C / 0 / 512MB / 100MB | MiniMax Image-01 | prompt → ≤5 比例 | 10–30s | **中** — 实验性 API |
| `openai_image` | **API** | 1C / 0 / 512MB / 100MB | gpt-image-2（OpenAI） | prompt → 1024²–1536² | 10–30s | **中** — OpenAI API |
| `recraft_image` | **API** | 1C / 0 / 512MB / 100MB | Recraft V4/V4-pro（fal.ai） | prompt → PNG 或 SVG | 10–20s | **中** — 适合 logo/SVG |
| `pexels_image` | **API** | 1C / 0 / 256MB / 50MB | 无（stock 库） | query → large2x JPG | <2s | **中** — 免费 stock |
| `pixabay_image` | **API** | 1C / 0 / 256MB / 50MB | 无（stock 库） | query → ≤1280px JPG | <2s | **中** — 免费 stock |
| `code_snippet` | 未声明 | 1C / 0 / 256MB / 50MB | Pygments + Pillow | 100-500 行代码 → PNG | <1s | **高** — 浏览器 Canvas 完全可做 |
| `diagram_gen` | 未声明 | 1C / 0 / 256MB / 50MB | Mermaid CLI 或 Pillow | mermaid → PNG | 2–10s | **中** — mermaid.js 可客户端化 |
| `math_animate` | **LOCAL** | 2C / 0 / 1GB / 500MB | ManimCE + FFmpeg + LaTeX | scene_code → mp4/gif | 5s–120s | **低** — Manim+LaTeX 全栈 + 用户代码执行 |

## B. `tools/character/character_animation.py`

| 工具 | Runtime | CPU/GPU/内存/磁盘 | 依赖 | I/O 量级 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `character_spec_generator` | 未声明 | 1C / 0 / 128MB / 10MB | 无 | 角色列表 → character_design JSON | <50ms | **高** — 纯 Python 数据变换 |
| `svg_rig_builder` | 未声明 | 1C / 0 / 128MB / 10MB | 无 | design → rig_plan JSON | <50ms | **高** — 纯数据变换 |
| `pose_library_builder` | 未声明 | 1C / 0 / 128MB / 10MB | 无 | rig_plan → pose_library JSON | <50ms | **高** — 纯数据变换 |
| `action_timeline_compiler` | 未声明 | 1C / 0 / 128MB / 10MB | 无 | scene_plan → action_timeline JSON | <50ms | **高** — 纯数据变换 |
| `character_rig_renderer` | 未声明 | 1C / 0 / 128MB / 50MB | HTML + GSAP；可选 Playwright + ffmpeg | timeline → preview.html | <100ms HTML | **中** — HTML 路径零成本 |
| `character_animation_reviewer` | 未声明 | 1C / 0 / 128MB / 10MB | schema 校验 | 3 个 artifact → QA report | <100ms | **高** — 纯 schema 校验 |

## C. `tools/enhancement/`

| 工具 | Runtime | CPU/GPU/内存/磁盘 | 依赖模型 | I/O 量级 | 单次耗时 | 客户端友好度 |
|---|---|---|---|---|---|---|
| `upscale` | **LOCAL_GPU** | 2C / **2GB VRAM** / 4GB RAM / 2GB | Real-ESRGAN (~65MB) + GFPGAN (~350MB) | 图像 → 4× 输出 | 图像 2-10s；视频按帧×N | **低** — 大权重 + VRAM + 视频逐帧 |
| `face_restore` | **LOCAL_GPU** | 2C / **2GB VRAM** / 2GB RAM / 1GB | CodeFormer (~350MB) 或 GFPGAN (~350MB) | 单图 → 2× 上采样人脸 | 1-5s/张 | **低** — 大权重 + VRAM |
| `bg_remove` | **HYBRID** | 2C / 0 / 2GB RAM / 500MB | rembg（U²-Net ~170MB ONNX） | 单图 → 透明 PNG | 0.5-3s/张（CPU） | **中** — 客户端 WASM ONNX 可行 |
| `color_grade` | 未声明 | 2C / 0 / 1GB RAM / 2GB | FFmpeg 滤镜 | 视频 → 视频 | 时长 × 0.3-1× | **高** — 浏览器 WebGL 可替代 |
| `face_enhance` | 未声明 | 2C / 0 / 1GB RAM / 2GB | FFmpeg 滤镜 | 视频 → 视频 | 时长 × 0.3-1× | **高** — 浏览器 WebGL 可替代 |
| `eye_enhance` | 未声明 | 4C / 0 / 2GB RAM / 4GB | MediaPipe Face Mesh + OpenCV | 视频逐帧 → 视频 | 时长 × 0.5-1× | **中** — MediaPipe 浏览器原生 |

---

## 真正 GPU 重负载（保留服务端）

| 工具 | 模型大小 | VRAM 需求 | 客户端可行性 |
|---|---|---|---|
| `comfyui_image` | FLUX 2 NVFP4 + Mistral TE + VAE ~10-15GB | 8GB+ | 浏览器不可行 |
| `local_diffusion` | SD 2.1-base ~5GB | 4GB+ | 浏览器 WASM 跑不动 |
| `upscale` | Real-ESRGAN ~65MB / GFPGAN ~350MB | 2GB+ | 视频路径不可行 |
| `face_restore` | CodeFormer / GFPGAN ~350MB | 2GB+ | 同上 |
| `math_animate` | Manim + LaTeX + 用户 Python 执行 | 无 | 客户端装不动 + 安全风险 |

---

## 可明确剥离到客户端

### 1. 纯 JSON 数据规整类（全部可移到浏览器 JS，零依赖）

- `character_spec_generator`
- `svg_rig_builder`
- `pose_library_builder`
- `action_timeline_compiler`
- `character_animation_reviewer`

### 2. 轻量渲染类（可纯客户端 / WebGL / Canvas / WebCodecs）

- `code_snippet`（shiki + Canvas 替代 Pygments + Pillow）
- `color_grade`（WebGL 着色器 / CSS filter）
- `face_enhance`（基础 WebGL 操作）
- `character_rig_renderer` HTML/GSAP 主路径
- `image_selector` 评分路由

### 3. 可客户端 + 服务端双跑（中等友好）

- `bg_remove`（U²-Net ONNX ~170MB，CPU 也跑得动）
- `eye_enhance`（MediaPipe Face Mesh 浏览器原生支持）
- `diagram_gen`（mermaid.js 替代 mmdc CLI）

### 4. API 类工具

- `pexels_image`、`pixabay_image`：浏览器直连（免费 stock）
- 其他 API 工具：浏览器可直连但需 BFF 代理 key 或 OAuth

---

## 关键结论

- **剥离收益最大**：5 个 character_animation JSON 工具 + reviewer、`code_snippet`、`color_grade`、`face_enhance`、`character_rig_renderer` HTML/GSAP 主路径、`image_selector`
- **中等候选**（需 WASM/JS 重写）：`bg_remove`、`eye_enhance`、`diagram_gen`
- **绝不动**：5 个 GPU 大模型（`comfyui_image`、`local_diffusion`、`upscale`、`face_restore`、`math_animate`）
- **执行用户代码安全**：`math_animate` 接收并执行用户 Python，必须留在服务端沙箱
- **API 类**：建议统一 BFF 代理层注入 key，浏览器直连
