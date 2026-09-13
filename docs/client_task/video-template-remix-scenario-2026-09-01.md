# video-template-remix 业务场景：服务端/客户端精化拆分

**日期**：2026-09-01
**业务场景**：用户找到多条对标视频（链接或文件）→ 上传到 OM 平台拆解 → 上传对应要修改的图片 → 描述期望效果 → OM 平台返回编排好的脚本 → 用户的 GUI 客户端用 FFmpeg 本地渲染新视频。

**对应的流水线**：`pipeline_defs/video-template-remix.yaml`（**默认流水线**）
**核心约束**（来自 manifest `metadata.remix_rules`）：
- **preserve**: shot_boundaries, shot_durations, pacing, transitions, subtitles, subtitle_position, source_audio, loudness
- **replace**: explicitly_approved_asset_slots_only
- **delete**: only_explicitly_approved_slots
- **forbid**: carbon_copy_without_rights, silent_slot_substitution, unapproved_audio_replacement, duration_drift

→ 这是**纯时间线替换型**流水线，本质是"用 FFmpeg 把对标视频的若干时间段切出来，叠加用户上传的图片，保留原音轨和字幕位置"。

---

## 1. 这个场景下的"服务端编排脚本"到底长什么样

回传给客户端的产物是 **`edit_decisions` + `asset_manifest` + `render_report` 三个 JSON 工件**，具体内容（基于 `schemas/artifacts/`）：

```jsonc
// edit_decisions (裁剪/替换/字幕/音轨的完整指令)
{
  "version": "1.0",
  "render_runtime": "ffmpeg",              // ★ 锁定为 ffmpeg，由客户端执行
  "composition_mode": "templated",
  "compose_target": { "width": 1080, "height": 1920, "fps": 30, "fit": "cover" },
  "cuts": [
    {
      "id": "cut_001",
      "source": "assets/reference/source.mp4",   // 对标视频路径
      "in_seconds": 0.0, "out_seconds": 3.2,      // 保留的源片段
      "transform": { "scale": 1.0, "position": "center",
                     "animation": "ken-burns-slow-zoom" },
      "overlay": { "asset_id": "user_img_001" }, // 用户上传的图片作为覆盖
      "transition_in": "fade", "transition_duration": 0.3
    },
    /* ... 更多 cut ... */
  ],
  "audio": {
    "narration": null,                         // ★ 保留源音频
    "music":   { "asset_id": null, "volume": 0.0, "ducking": false },
    "sfx":     []
  },
  "subtitles": {
    "enabled": true, "style": "sentence",
    "source": "assets/source.srt",
    "position": "bottom-center",
    "max_words_per_line": 8
  }
}
```

```jsonc
// asset_manifest (用户上传的图片 + 引用源资产)
{
  "version": "1.0",
  "assets": [
    { "id": "user_img_001", "type": "image", "path": "assets/user/uploads/img_001.png",
      "source_tool": "user_upload", "scene_id": "cut_001" },
    { "id": "user_img_002", "type": "image", "path": "assets/user/uploads/img_002.png",
      "source_tool": "user_upload", "scene_id": "cut_003" },
    { "id": "source_ref_video", "type": "video", "path": "assets/reference/source.mp4",
      "source_tool": "video_downloader", "scene_id": "all" }
  ]
}
```

```jsonc
// render_report (客户端渲染完成后回传的产物记录)
{
  "version": "1.0",
  "outputs": [{
    "path": "/user/outputs/final.mp4",
    "format": "mp4", "codec": "h264", "audio_codec": "aac",
    "resolution": "1080x1920", "fps": 30,
    "duration_seconds": 32.4, "file_size_bytes": 18400000
  }],
  "render_time_seconds": 12.3,
  "verification_notes": ["source 32.0s vs output 32.4s within tolerance"]
}
```

**客户端拿到这三个 JSON 后做的事**：

1. 解析 `edit_decisions.cuts[]`，按 `source + in/out + transform + overlay` 生成 FFmpeg filtergraph
2. 把 `asset_manifest` 中的资源路径映射到本地（用户上传的图、对标视频的本地缓存）
3. 烧字幕（`edit_decisions.subtitles`）
4. 混音轨（`edit_decisions.audio`，默认 preserve source_audio）
5. 写出最终 MP4，回传 `render_report` 到 OM 平台

---

## 2. 这个场景下的真实负载分布

### 2.1 服务端必须做的事（不可剥离）

| 阶段 | 工具 | 负载特征 | 备注 |
|---|---|---|---|
| **拆解对标视频** | `video_analyzer`、`video_downloader`、`scene_detect`、`frame_sampler`、`transcriber` | ffmpeg + PySceneDetect + faster-whisper，CPU/IO 中等 | 对标视频必须先在服务端下载/分析（用户不直接接触源） |
| **理解内容（可选）** | `video_understand`（CLIP/BLIP-2/LLaVA） | **GPU 重** | 只有当用户描述"理解含义"时才需要；本场景可以选 base 或不做 |
| **生成编排脚本** | Agent 生成 `edit_decisions` JSON | 纯逻辑（Agent LLM 调用 + skill 读取） | **编排本身不重** |
| **校验工件** | `composition_validator`、schema 校验 | 极轻量 | — |
| **回传** | `asset_upload` / `rsync_upload` | 网络 | 必须服务端（MCP/BFF 入口） |

### 2.2 客户端必须做的事（不可在服务端完成）

| 任务 | 工具 / 能力 | 客户端承载方式 |
|---|---|---|
| **接收用户上传** | GUI 文件选择器 → 走 `asset_upload` / 分片协议 | 客户端发起 |
| **FFmpeg 渲染** | `video_compose.py` FFmpeg-only 路径（`compose` / `encode` / `burn_subtitles` / `overlay`） | **客户端原生 FFmpeg binary**（不是 FFmpeg.wasm，GUI 客户端打包二进制更稳定） |
| **最终视频输出** | 客户端写本地文件系统 | — |

### 2.3 这个场景下"重负载"工具的重新评估

我之前给的全局分析，**对这个场景做了过度概括**。针对 `video-template-remix`：

| 全局重负载 | 在本场景的真实定位 |
|---|---|
| **Remotion 渲染栈**（870MB node_modules） | **本场景不用**。`compose-director.md` 明确说："Prefer FFmpeg/video_compose for source-faithful assembly"。`edit_decisions.render_runtime` 在本场景 = `"ffmpeg"`。**Remotion 全部不沾手**。 |
| **HyperFrames 渲染** | 同上，本场景不用 |
| **LTX-2 22B / WAN 14B / Hunyuan / FLUX / SD 5GB / Wav2Lip / SadTalker / LLaVA** | **本场景完全不用**。本场景是替换型，不是生成型。用户没要求生成新视频/新音频。 |
| **视频分析（transcriber/scene_detect/frame_sampler/video_analyzer）** | **必须**。这是 OM 的核心价值——拆解对标视频的时间线、字幕、场景。 |
| **FFmpeg 渲染** | **客户端承担**。OM 服务端只需要生成指令，**不实际渲染**。 |
| **本地 TTS/音乐/翻译** | **本场景基本不用**。源音轨保留，不生成新音乐/新 TTS。 |

**关键洞察**：这个业务场景是 OM 13 条流水线中**最轻量的一条**，但仍然是核心价值所在（拆解对标视频 + 编排替换）。

---

## 3. 服务端/客户端精化职责划分

### 服务端（OM 平台）核心职责

```
输入：
  - 多条对标视频 URL 或文件（用户上传或链接）
  - 多张用户图片（用户上传）
  - 描述文本（"我要这种感觉"）

处理：
  1. 视频下载/转码 → projects/<id>/assets/reference/source.mp4
  2. 视频分析：
     - scene_detect → 切分 shot
     - frame_sampler → 关键帧
     - transcriber → 字幕（带时间戳）
     - video_analyzer → 综合 VideoAnalysisBrief
     - 可选 video_understand（轻量 base 模式）→ 风格/主题理解
  3. 编排：
     - Agent 根据 brief + scene_plan + asset_manifest 生成 edit_decisions
     - render_runtime = "ffmpeg"（由本场景决定）
     - 校验时长漂移 ≤ 1 frame / shot
  4. 打包产物：
     - edit_decisions.json
     - asset_manifest.json
     - reference source video (或其切片包)
     - subtitles.srt（如果原视频有）

输出：
  - 编排脚本包（4 个文件）
```

### 客户端（GUI）核心职责

```
输入：
  - 服务端返回的编排脚本包
  - 用户上传的多张图片（本地缓存）

处理：
  1. 解析 edit_decisions + asset_manifest
  2. 资源本地化：
     - source video 路径
     - 用户图片路径（按 asset_id 匹配 cut.overlay.asset_id）
  3. FFmpeg 渲染：
     - 对每个 cut：ffmpeg -ss in -to out -i source -i user_img \
                       -filter_complex "[0:v]scale,crop[bg];[1:v]scale,overlay[fg];[bg][fg]overlay,format=yuv420p[v]" \
                       -map "[v]" -map 0:a out_001.mp4
     - concat 所有 cut
     - 烧字幕（filtergraph subtitles）
     - 混音（preserve source audio，无 music）
  4. 输出 final.mp4
  5. 回传 render_report 到 OM 平台

输出：
  - 最终视频（本地）
  - render_report.json（回传）
```

---

## 4. 关键设计要点

### 4.1 服务端只生成"FFmpeg 命令模板"，不实际渲染

`edit_decisions` JSON 已经定义了所有 FFmpeg 渲染所需的元数据：
- `cuts[].source / in_seconds / out_seconds` → `-ss / -to / -i`
- `cuts[].transform.scale / position / animation` → filter `scale, crop, zoompan`
- `cuts[].overlay.asset_id` → `overlay` filter
- `cuts[].transition_in / transition_duration` → `xfade` filter
- `subtitles.*` → `subtitles` filter
- `audio.*` → `-map` 和 `amix` filter

→ **客户端只要有一个 FFmpeg binary + JSON 解析器**，就能完整渲染。不需要再下放 video_compose 这个 Python 工具。

### 4.2 服务端资源压力集中在"分析"，不是"渲染"

| 任务 | 估算（per 项目） |
|---|---|
| 下载对标视频 | 10–60 秒（受网络） |
| 提取音频 + 转录 | 视频时长 × 0.3–0.5（faster-whisper base） |
| 场景检测 | 视频时长 × 0.2（PySceneDetect） |
| 关键帧采样 | 视频时长 × 0.1 |
| 综合分析 (video_analyzer) | 30–120 秒 |
| VLM 理解（可选） | 1–3 分钟（CUDA） |
| 生成 edit_decisions | 5–30 秒（Agent LLM） |
| **服务端总时长** | **5–10 分钟（无 VLM）/ 10–15 分钟（含 VLM）** |
| **客户端 FFmpeg 渲染** | 视频时长 × 0.5–1.0（仅本地 IO + 编解码） |

→ 服务端的真实负载是**分析（CPU 中等）**，不是渲染（CPU 重）。这正好与"分析留在服务端、渲染去客户端"的策略匹配。

### 4.3 多对标视频的处理

用户上传多条对标视频 → 服务端需要决定如何融合多个参考：
- **选项 A**：选一条作为主对标，其余作为风格参考（用 video_understand 提取 style_profile）
- **选项 B**：多条拼接（按章节）
- **选项 C**：多条中"投票"出最匹配的 cut 结构（用 scene_detect 对齐）

→ 这是 Agent 在 `idea-director` 阶段的决策，由 `decision_log` 记录，**不影响服务端/客户端职责划分**。

### 4.4 用户图片与 cut 的匹配

```jsonc
// scene_plan 示例（节选）
{
  "scenes": [
    { "id": "s01", "start": 0.0, "end": 3.2,
      "asset_slot": { "type": "image", "required": true,
                      "description": "hero shot of the product" } },
    { "id": "s02", "start": 3.2, "end": 6.8,
      "asset_slot": { "type": "preserve_source" } },
    { "id": "s03", "start": 6.8, "end": 10.4,
      "asset_slot": { "type": "image", "required": true,
                      "description": "feature highlight" } }
  ]
}

// 资产匹配后：
{
  "cuts": [
    { "id": "c01", "source": "source.mp4", "in_seconds": 0.0, "out_seconds": 3.2,
      "overlay": { "asset_id": "user_img_001" } },  // 用户上传图 1 匹配 s01
    { "id": "c02", "source": "source.mp4", "in_seconds": 3.2, "out_seconds": 6.8,
      "transform": { "animation": "ken-burns-slow-zoom" } },  // s02 不替换，原画面动效
    { "id": "c03", "source": "source.mp4", "in_seconds": 6.8, "out_seconds": 10.4,
      "overlay": { "asset_id": "user_img_002" } }   // 用户上传图 2 匹配 s03
  ]
}
```

→ 服务端在 `asset-director` 阶段做匹配（基于用户上传的图片数量 + 用户描述）；不匹配时 surface 给用户决定（"你只上传了 2 张图，但有 3 个 replace slot，怎么办？"）。

### 4.5 用户描述如何生效

用户描述（"我要电影感、加个 cinematic LUT 色调、保留原字幕但改成英文"）由 Agent 在 `idea`/`script` 阶段读取，落地到：

- `edit_decisions.cuts[].transform.crop` / `position` / `animation`
- `edit_decisions.cuts[].transition_in` / `transition_duration`
- 未来可能的 `edit_decisions.metadata.lut` / `color_grade` 字段

→ 描述主要影响 **FFmpeg filter 选择**，不需要额外的服务端计算。

---

## 5. 落地建议（针对 video-template-remix 场景）

### 5.1 服务端必须保留的工具（仅本场景）

| 工具 | 必选 |
|---|---|
| `video_analyzer` | ✅ 核心价值 |
| `video_downloader` | ✅ 用户给链接时必用 |
| `scene_detect` | ✅ 切 shot |
| `frame_sampler` | ✅ 抽关键帧 |
| `transcriber` (tiny/base/small) | ✅ 字幕对齐 |
| `composition_validator` | ✅ 校验 |
| `asset_upload` / `asset_upload_chunk` | ✅ 协议入口 |
| `read_session_asset*` | ✅ BFF 出口 |
| `rsync_upload` | ✅ 发布 |
| Agent LLM | ✅ 编排核心 |
| **可选** `video_understand` (base) | 仅当用户描述"理解风格/含义"时启用 |
| **可选** `image_selector` | 仅当用户上传多于 cut 数、需要建议分配时 |

### 5.2 服务端明确不需要的工具（本场景）

- ❌ **所有 Remotion 相关**（remotion-composer / remotion_caption_burn / video_compose 的 Remotion 路径）
- ❌ **所有 HyperFrames 相关**（hyperframes_compose）
- ❌ **所有 LOCAL_GPU 视频生成**（ltx_video_local、comfyui_video、wan_video、hunyuan、cogvideo）
- ❌ **所有付费 API 视频生成**（sora/veo/seedance/kling/runway 等 16 个）
- ❌ **所有付费 API 图像生成**（flux_image、openai_image、google_imagen 等）
- ❌ **所有本地 GPU 图像生成**（comfyui_image、local_diffusion）
- ❌ **所有 TTS**（piper/kokoro/voicebox/elevenlabs 等）—— 本场景不生成新音轨
- ❌ **所有音乐生成**（suno/music_gen/music_gen_local）
- ❌ **所有图像增强 GPU 工具**（upscale、face_restore、comfyui_image）
- ❌ **所有数字人**（kling_avatar/lip_sync/talking_head）
- ❌ **math_animate**（执行用户 Python 代码，安全边界）

### 5.3 客户端需要的能力

| 能力 | 实现方式 |
|---|---|
| 接收编排脚本包 | HTTP 下载 zip 或解析 MCP 响应 |
| 解析 edit_decisions + asset_manifest | 内置 JSON 解析器（任何语言） |
| 调用 FFmpeg | **打包 FFmpeg 二进制**（不是 FFmpeg.wasm，GUI 客户端二进制更稳定高效） |
| 用户上传图片 | GUI 文件选择器 → 走 `asset_upload` 分片协议 |
| 回传 render_report | HTTP POST 到 OM 平台 |
| （可选）回传最终视频 | `rsync_upload` 或分片上传 |

### 5.4 FFmpeg 命令模板生成器

客户端需要一个**轻量级 FFmpeg 命令生成器**，输入是 `edit_decisions` JSON，输出是 `ffmpeg` shell 命令。核心映射：

| edit_decisions 字段 | FFmpeg filter / 参数 |
|---|---|
| `cuts[].source` | `-i <path>` |
| `cuts[].in_seconds` / `out_seconds` | `-ss` / `-to`（或 trim filter） |
| `cuts[].transform.scale` | `scale=W:H` |
| `cuts[].transform.crop` | `crop=W:H:X:Y` |
| `cuts[].transform.animation: ken-burns-slow-zoom` | `zoompan=z='min(zoom+0.0015,1.5)':d=...` |
| `cuts[].overlay.asset_id` | `overlay=x:y` filter |
| `cuts[].transition_in` / `transition_duration` | `xfade=transition=...:duration=...:offset=...` |
| `subtitles.*` | `subtitles=filename=...:force_style=...` filter |
| `audio.music.asset_id` | `-i <music>` + `amix=inputs=2:duration=first` |
| `compose_target` | `-r <fps>`、`-s WxH`、`-aspect` |

→ 这个生成器可以是 **Python 100 行**、**Go 150 行**、**Node.js 80 行** 的小工具，**完全不需要 OpenMontage 的全部 BaseTool 体系**。

---

## 6. 总结：这个场景下的最优架构

```
┌──────────────────────────────────────────────────────────────────┐
│            服务端 (OpenMontage) — 仅 video-template-remix         │
│  角色: 拆解对标视频 / 编排替换 / 生成 FFmpeg 指令脚本            │
├──────────────────────────────────────────────────────────────────┤
│ 输入:  对标视频 URL/文件 + 用户图片 + 描述                       │
│                                                                  │
│ 必跑工具:                                                         │
│   video_analyzer → video_downloader → scene_detect →             │
│   frame_sampler → transcriber → composition_validator            │
│                                                                  │
│ Agent 编排:                                                       │
│   idea → script → scene_plan → asset_manifest → edit_decisions   │
│   (render_runtime = "ffmpeg", composition_mode = "templated")    │
│                                                                  │
│ 输出: 编排脚本包 (edit_decisions + asset_manifest + 源视频包)   │
└──────────────────────────────────────────────────────────────────┘
                            ▲ ▼ (编排脚本 JSON + 源视频 + 字幕)
┌──────────────────────────────────────────────────────────────────┐
│            客户端 (GUI + FFmpeg binary)                           │
│  角色: 渲染新视频（执行 FFmpeg 命令）                            │
├──────────────────────────────────────────────────────────────────┤
│ • 接收编排脚本包                                                  │
│ • 解析 edit_decisions → 生成 FFmpeg filtergraph                   │
│ • 调用本地 FFmpeg 二进制执行渲染                                  │
│ • 输出 final.mp4 到本地                                          │
│ • 回传 render_report.json 到 OM 平台                             │
└──────────────────────────────────────────────────────────────────┘
```

**这个场景下服务端真正需要承担的计算负载：**

| 类别 | 工具 | 真实成本 |
|---|---|---|
| 下载 | `video_downloader` | I/O bound，10–60s |
| 拆解 | `scene_detect + frame_sampler + transcriber` | **CPU 中等**，合计视频时长 × 0.5 |
| 综合 | `video_analyzer`（编排器） | CPU 中等，30–120s |
| 可选 VLM | `video_understand` (base) | **GPU 中等**（10s/帧），仅在用户要求"理解含义"时 |
| 编排 | Agent LLM | Token 消耗，秒级 |
| **渲染** | **0** —— 全部在客户端 | **0** |

**这个场景下服务端"绝对不要做"的事：**

- ❌ 不要跑 Remotion / HyperFrames
- ❌ 不要跑 LTX-2 / WAN / Hunyuan / FLUX / SD / Wav2Lip
- ❌ 不要跑 TTS / 音乐生成（除非用户明确要求替换音轨）
- ❌ 不要跑 upscale / face_restore / bg_remove（用户上传的图已定稿）
- ❌ 不要做客户端的 FFmpeg 渲染

**关键收益**：
1. **服务端零渲染**——避免 Remotion 870MB node_modules + headless Chrome 的最大瓶颈
2. **客户端零 GPU**——纯 FFmpeg binary + JSON 解析，普通笔记本可跑
3. **隐私保护**——对标视频和用户图片全程不出客户端（除非用户主动上传）
4. **带宽节省**——客户端只下载编排脚本（KB 级）+ 源视频（若本地无）；不下载中间产物

---

## 7. 与全局分析的差异

| 维度 | 全局分析（13 条流水线） | 本场景（video-template-remix） |
|---|---|---|
| 服务端 GPU 需求 | 中（多模型共用） | **低**（只用 transcriber、scene_detect，可选 video_understand base） |
| 服务端 Node 需求 | 高（Remotion/HyperFrames） | **0** |
| 服务端真实负载 | Remotion 渲染 + VLM + 生成 | **视频分析 + 编排** |
| 客户端能力门槛 | 高（FFmpeg.wasm / ONNX.wasm / GPU） | **低**（FFmpeg binary + JSON 解析） |
| 16 个 API 视频生成工具 | 多数下沉客户端 | **完全不用** |
| TTS / 音乐工具 | 多数下沉客户端 | **完全不用**（保留源音轨） |

→ **针对这个核心场景，服务端的最简化形态就是：下载器 + 拆解器 + Agent LLM + 协议入口**。客户端就是：**GUI + FFmpeg binary**。整套系统的"重负载"被收敛到分析侧和编排侧，渲染侧彻底由客户端承担。
