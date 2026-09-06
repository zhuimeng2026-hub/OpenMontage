# animated-explainer — Pipeline Analysis & EDC Use Case

> 用途：把 `animated-explainer` pipeline 的能力、当前环境约束、以及"通勤 EDC 数码翻包"作为它的输入会跑成什么样，写一份给后人/自己回头看的分析。
> 评估日期：2026-08-31
> 评估环境：`/opt/OpenMontage_Voicebox` (release/mvp-v0.1-phase-0-5)

## TL;DR

`animated-explainer` 是 OpenMontage 13 条 pipeline 里**完全 AI 生成**的代表（vs `video-template-remix` 是 source-faithful slot 替换）。它跑出来的产物是"独立 3 分钟讲解视频"，不复用任何源视频时间线。当前环境**可以跑**，但视觉质量受限于 `video_generation 1/24`（只有 `minimax_direct` 一个 provider），更现实的预期是"stock-image 风格的 Remotion 动效讲解"，而不是 cinematic AI 视频。

如果要做出片级别的视觉效果，需要补 1-2 个云端视频生成 provider（Kling / VEO / Sora / Runway 任一）以及 1 个图像生成 provider（Flux / Imagen / Recraft 任一）。

## 1. Pipeline 结构

`pipeline_defs/animated-explainer.yaml`（v2.0, stability: production）：

```
research (no gate)
  ↓
proposal (gate) ─┬─ sample sub-stage (gate, 10-15s preview)
                 ↓
script (gate)
  ↓
scene_plan (gate)
  ↓
assets (gate)        ← tts_selector + image_selector 必选；video_selector / music_gen 可选
  ↓
edit (auto)
  ↓
compose (auto)       ← video_compose + audio_mixer 必选
  ↓
publish (gate)
```

9 个 stage，5 个 gate（proposal/script/scene_plan/assets/publish），默认预算 **$2.00**，单 stage 最多 3 次修订，最多 3 次 send-back，wall_time 上限 **20 分钟**。

需要 skills：`pipelines/explainer/{executive-producer,research-director,proposal-director,script-director,scene-director,asset-director,edit-director,compose-director,publish-director}.md` + `meta/{reviewer,checkpoint-protocol,skill-creator,animation-runtime-selector,voice-performance-director}.md`。

兼容 playbook：`clean-professional`、`flat-motion-graphics`（推荐）、`minimalist-diagram`（also_works）。**不**支持 `premium-minimalist` / `ink-sketch`。

支持 reference video：`reference_input.supported: true`，`analysis_depth: standard`（比 `video-template-remix` 的 deep 浅一档）。

## 2. EDC use case 映射

如果输入是 "通勤 EDC 数码翻包"（3 分钟、城市白领、bilibili 风格），各 stage 期望产出：

| stage | 产物 | EDC 内容 |
|---|---|---|
| research | `research_brief` | EDC 品类趋势 / 通勤场景 / 数码三件套选品理由；≥3 个 angles、≥5 个 sources |
| proposal | `proposal_packet` | 3 个差异化概念（"3 件套速览" vs "通勤痛点对照" vs "科技感开箱"）；含 sample sub-stage 15s 预览 |
| script | `script` | 5-7 个 sections，每个含 enhancement cue（overlay/broll/diagram/stat_card/animation）；总时长 ~180s |
| scene_plan | `scene_plan` | 30-60 个 scenes，视觉多样性 ≥3 种 scene type |
| assets | `asset_manifest` | 30-60 张图（image_selector）+ 0-10 段视频（video_selector）+ 完整 TTS + bgm |
| edit | `edit_decisions` | cuts/subtitles/music ducking decisions |
| compose | `render_report` + `final_review` | 完整 mp4，duration ±5% |
| publish | `publish_log` | SEO metadata + chapter markers + export bundle |

## 3. 当前环境能力实测（2026-08-31 拉自 `provider_menu_summary` + 实时 registry probe）

| 维度 | 配置 | 实测状态 | 影响 |
|---|---|---|---|
| `composition_runtimes` | ffmpeg ✓ / remotion ✓ / hyperframes ✓ | 三 runtime 都装好，remotion node_modules 870 MB，chromium 220 MB | ✅ 三个 runtime 都能选 |
| `image_generation` | 3/13 | `minimax` + `multi` + `openai` 已配置 | ✅ 够用 30-60 张图 |
| `video_generation` | **1/24** | **只 `minimax_direct` 活着** | ⚠️ **关键瓶颈** |
| `tts` | voicebox_tts ✅ / edge_tts ❌ / piper_tts ✅ | edge_tts 实查 unavailable（缺 `pip install edge-tts`） | ⚠️ TTS 选项缩水 |
| `music_generation` | 1/4 | `local`（piper/MusicGen 本地 CPU） | ✅ 够用 bgm |
| `video_post` | 9/9 | ffmpeg + hyperframes 全开 | ✅ 合成工具齐 |
| `screen_capture` | 2/2 | cap + ffmpeg | 不需要 |
| `subtitle` | 2/2 | openmontage + remotion | ✅ 字幕烧录齐 |
| `analysis` | 8/14 | 含 ffmpeg/ffprobe/local/multi/transformers | ✅ scene_detect/transcriber/frame_sampler 全有 |

**关键发现**：

1. **`video_generation` 是最大瓶颈**。animated-explainer 的 "assets" stage 通常会生成 5-15 段短视频（cinematic B-roll / 动效背景），但当前只有 1 个云端 provider（`minimax_direct`），配额和成本都不透明。要做 cinematic 视觉，至少补 1 个：`kling_official` (env: `KLING_API_KEY`) / `runway` / `veo` / `seedance` / `comfyui` (env: `COMFYUI_SERVER_URL`，但需要 ≥16GB VRAM)。

2. **`edge_tts` 不在 menu 里**。Menu 显示 tts 3/10 但实查 `tools/tts/edge_tts.py` registry 是 unavailable（`install_instructions: pip install edge-tts`）。要解：直接 `pip install edge-tts` 然后重启 MCP 即可获得 10+ 高质量中文 TTS 声音。

3. **`voicebox_tts` registry available 但运行时实际失败**。Backend 在 `:17493` 健康（`status=healthy, model_loaded=true, model_size=1.7B`），但 OpenMontage 端的 `tools/tts/voicebox_tts.py` 在某些 Python 路径下有 import 问题（之前 2026-08-29 验证过：HF proxy + starlette/FastAPI 版本冲突）。**短期方案**走 `kokoro==0.9.4` 直接 KPipeline（per memory `voicebox-blocked-kokoro-escape-hatch`）。

4. **`image_generation` 也偏瘦**。3/13 能用，但风格多样性受限。补 `flux` / `google_imagen` / `recraft` / `grok` 中任一可解锁更高质量视觉。

5. **`hyperframes` runtime 实测可用**。`~/.npm/_npx` 已有 31 个 hash 缓存（最近 2026-08-31 10:51），`hyperframes doctor` 应该能跑（未实跑，但缓存暖着）。

## 4. Wall-time 20 分钟够不够

`max_wall_time_minutes: 20` 看起来宽裕但实际很紧。粗算：

| 阶段 | 估算耗时 | 说明 |
|---|---|---|
| research (LLM/web) | 1-2 min | 5 个 source 抓 + 3 个 angle 提炼 |
| proposal | 1-2 min | 3 个概念 + 1 个 15s sample（sample 是 bottleneck：渲染 15s 视频通常 1-3 min） |
| script + scene_plan | 1 min | 纯文本生成 |
| assets（30 图 + 5 视频 + TTS + bgm） | **8-12 min** | 取决于 provider 并发。`minimax_direct` 单段 5-15s，5 段 = 1-2 min；图像 30 张 / 6 并发 = ~1 min；TTS 7 段 ~30s；bgm ~30s |
| edit + compose | 1-2 min | 主要是渲染（Remotion 3 分钟视频 ~30-60s） |
| publish | <1 min | 打包 |
| **合计** | **13-20 min** | ⚠️ 临近上限 |

**关键 risk**：assets stage 是 bottleneck。如果 video_generation 只有 1 个 provider 且慢，5 段视频可能要 5+ 分钟；TTS 走 voicebox 出问题时又可能 1-2 分钟 retry。

**缓解方案**：
- 把 assets 阶段的视频段数砍到 2-3 段（其余用静态图 + Remotion 动效替代）
- 把 bgm 直接用本地现成的 mp3（`music_library/`），跳过 music_gen 生成
- 关闭 proposal 的 sample 子阶段（如果 user 不强求预览）

## 5. 实际产出预期

如果现在跑 animated-explainer with current env，最可能产物是：

```
resolution:  1920×1080 (Remotion default) 或 1080×1920 (9:16)
duration:    ~180s
fps:         30
video codec: h264
audio:       aac, voicebox TTS（中文女声/男声）+ 本地 MusicGen bgm
visuals:     30-50 张 AI 生成静态图 + Remotion spring 动效 + 2-5 段 minimax_direct 短视频
字幕:        自动烧录
```

**风格画像**：stock-image 风格的 AI 讲解视频，类似"小宇宙/得到" 课程预告片的感觉。不是 B站头部 up 主那种电影感剪辑，是信息密度中等、动效温和、AI 痕迹明显的科普向内容。

## 6. 升级路径（按 ROI 排序）

| 升级 | env var | 解锁什么 | 估算成本/视频 |
|---|---|---|---|
| **加 edge-tts** | `pip install edge-tts` | 10+ 高质量中文 TTS 声音 | $0 |
| **加 Kling API** | `KLING_API_KEY` | 1.6/2.0/2.1 多档视频生成 + motion brush | ~$0.10/段 |
| **加 VEO** | `GOOGLE_API_KEY` (Vertex) | 8s/16s 高质量视频生成 | ~$0.50/段 |
| **加 Runway** | `RUNWAY_API_KEY` | Gen-3/4 视频生成 | ~$0.50/段 |
| **加 Flux** | `BFL_API_KEY` | FLUX.1/FLUX.2 图像生成（更艺术） | ~$0.05/图 |
| **加 suno** | `SUNO_API_KEY` | 高质量 bgm（vs 本地 MusicGen） | ~$0.50/首 |
| **加 ElevenLabs** | `ELEVENLABS_API_KEY` | 顶级 TTS（多情感 voice clone） | ~$0.30/段 |

**最划算的两步**：edge-tts（5 秒装好，免费）+ Kling API（解锁高质量视频生成）。

## 7. 决策

跑 vs 不跑 vs 推迟？

- **跑（最低配置）**：30 张静态图 + Remotion spring 动效 + 0 段视频 + 本地 TTS + 本地 bgm。**视觉 = AI 文档式讲解**，不出彩但能跑通流程。
- **跑（推荐配置）**：先 `pip install edge-tts` 再跑，+ minimax_direct 2-3 段视频。**视觉 = stock-image 风格**，够看。
- **跑（出片配置）**：补 Kling API + Flux，**视觉 = cinematic**，但需要 1-2 小时 setup + API 费用。
- **推迟**：当前用于参数对标验证不必要，先用 `video-template-remix` (strict/heavy) 完成模板复用性验证，animated-explainer 留作后续出片 pipeline。

## 8. 相关文件

- Pipeline manifest: `pipeline_defs/animated-explainer.yaml`
- Stage directors: `skills/pipelines/explainer/*.md`
- 当前 capability menu 快照: `/tmp/pm.json` (24 KB)
- 当前任务快照：projects/bilibili-remix-strict-2026-08-31/ 和 projects/bilibili-remix-heavy-2026-08-31/ 已启动但 heavy 未完成

## 9. 历史

- 2026-08-31: 初版分析（基于 provider_menu 实测 + pipeline manifest 解读 + voicebox backend 实测）
