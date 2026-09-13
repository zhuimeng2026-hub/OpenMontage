# animated-explainer — Use Case Reference: 通勤 EDC 数码翻包

> 2026-08-31 评估的"如果拿 EDC 跑 animated-explainer 会长什么样"。

## 输入假设

```
topic:    通勤 EDC 数码翻包
audience: 城市白领 / 通勤族 / 数码 EDC 爱好者
tone:     真实评测 / 实用 / 口语化
style:    flat-motion-graphics (playbook)
platform: bilibili (实际 manifest enum 是 "generic" — bilibili 不在枚举里)
duration: 180s (3 分钟)
```

## 预期 9-stage 流水线产物

### research stage (无 gate)
- `research_brief.json`:
  - `data_points`: 8-12 条事实，每条带 source URL
  - `angles_discovered`: 3-5 个差异化角度
  - `sources`: 8-15 个 URL（bilibili / 知乎 / 小红书 / 36kr 等）
  - `audience_questions`: 5-8 条真实论坛问题

### proposal stage (gate)
- `proposal_packet.json`:
  - `concept_options`: 3 个差异化概念
    1. "3 件套速览" — fast-cut 风格，30 张图 + 5 段视频
    2. "通勤痛点对照" — problem-solution 结构，20 张图 + 3 段视频 + 2 diagram
    3. "科技感开箱" — slow-pace 高质量视频为主，5 张图 + 15 段视频
  - `selected_concept`: 取决于用户偏好
  - `cost_estimate`: $0.10-2.00 per concept
  - sample sub-stage (gate): 10-15s preview render

### script stage (gate)
- `script.json`:
  - 6-7 sections, ~1500-2000 中文字
  - 每 section 1-2 enhancement cue
  - voice_performance: conversational pace, sample_section_id 指向最具表演力的 section
  - word count ±10% of 180s target (~525 chars at conversational pace)

### scene_plan stage (gate)
- `scene_plan.json`:
  - 30-60 scenes
  - ≥3 种 scene type (overlay/broll/diagram/stat_card/animation)
  - 全部 scene 类型来自 Remotion scene_types catalog 或 HyperFrames registry
  - 没有连续 3 个同类型 scene
  - 完整 180s 时间线，无 gap/overlap

### assets stage (gate)
- `asset_manifest.json`:
  - 30-60 张图（image_selector）— 每张 3-5s 显示时长
  - 2-15 段视频（video_selector）— 每段 3-10s
  - 6-7 段 TTS（tts_selector）— 每 section 一段
  - 1 段 bgm（music_gen）
  - 全部 file path 真实存在
  - total cost ≤ budget

### edit stage (auto)
- `edit_decisions.json`:
  - cut decisions (scene_id → asset)
  - subtitle styling (per playbook)
  - music ducking (-12 dB during narration, -3 dB ambient)

### compose stage (auto)
- `render_report.json` + `final_review.json`:
  - render_runtime 锁定为 proposal 时选的 (FFmpeg / Remotion / HyperFrames)
  - output duration ±5% of target
  - ffprobe 验证 video/audio 都 OK
  - render_runtime 严格 = proposal 选的；**silently 换 runtime 是 CRITICAL governance violation**（manifest 第 245 行明文）

### publish stage (gate)
- `publish_log.json`:
  - SEO metadata (title / description / tags / chapters)
  - export bundle: video + metadata.json + thumbnail.jpg

## 实际产出预期 (per env check)

### 最好情况（推荐配置：edge-tts + minimax_direct）
```
resolution:  1920×1080
duration:    ~180s (±9s = 5%)
fps:         30
codec:       h264 + aac
visuals:     30-50 张 minimax/multi/openai 生成的图 + 2-3 段 minimax_direct 视频
animation:   Remotion spring/spring 缓动
narration:   edge-tts 中文女声 (zh-CN-XiaoxiaoNeural) 7 段
bgm:         本地 MusicGen 1 段
subtitles:   openmontage subtitle_gen 烧录
size:        ~30-60 MB
```

### 最低配置（当前现状）
```
visuals:     30-50 张 minimax/multi/openai 图，**0 段视频**（minimax_direct 配额/速度风险下可能自动退到 0）
animation:   Remotion spring 动效 + Ken Burns 平移（如果 ffmpeg fallback）
narration:   voicebox_tts / kokoro 直接 KPipeline
bgm:         本地 MusicGen
subtitles:   烧录
size:        ~20-40 MB
```

视觉风格画像：stock-image AI 讲解视频，类似得到/小宇宙课程预告片。

## 与 strict/heavy 路径对比

| 路径 | 输出文件名 | 复用源视频？ | 视觉变化 | 工作量 |
|---|---|---|---|---|
| strict | `bilibili-remix-strict-2026-08-31/renders/final_strict_slot.mp4` | 是（≈源视频） | 极小（仅声明 slot，未实际叠层） | 已完成 (86 MB) |
| heavy | `bilibili-remix-heavy-2026-08-31/renders/final_heavy_remix.mp4` | 是（视频轨 + 新音频） | 中（音频换 + 视频叠层） | 进行中 |
| **animated-explainer** | `bilibili-animated-edc-2026-08-31/renders/final.mp4` | **否** | **高（独立 3 分钟视频）** | 大（5 gate + 大量生成） |

## 推荐决策

**当前阶段不建议跑 animated-explainer**，原因：

1. video_generation 只 1 个 provider，出片质量上限低
2. wall_time 20 分钟预算紧，assets 阶段会爆
3. 我们已经在跑 strict + heavy 两条 video-template-remix 路径，再开 animated-explainer 闸门数量爆炸

**建议**：
- 先完成 strict + heavy 对比，作为"参数对标模板复用性"的有效性证据
- animated-explainer 留作后续"出片 pipeline"，先补 edge-tts + Kling API 再开跑

如果你确实现在就要跑，给我绿灯，我会：
1. 先 `pip install edge-tts`（5 秒）
2. 创建 `projects/bilibili-animated-edc-2026-08-31/`
3. 跑 9 stage，前 5 个 stage 每个 stage 闸门停下来等你批
4. 最终出 `final_animated_explainer_edc.mp4`

预计总耗时：30-60 分钟（取决于你批闸门的速度）+ $0.10-0.50 API 成本。

## 历史

- 2026-08-31: 初版 use case 参考
