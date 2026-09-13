# animated-explainer — Environment Capability Check (2026-08-31)

> 实时从 MCP `:8900` 拉 `provider_menu` + 单独 probe 各 TTS 工具的 registry status。
> 数字都是这次会话里实测，不是历史快照。

## A. Composition Runtimes (来自 get_provider_menu)

```
ffmpeg:     True
remotion:   True
hyperframes: True
```

三 runtime 都已安装：
- ffmpeg：PATH 在，HEVC decode/encode + libx264 验证过
- remotion：`remotion-composer/node_modules` 870 MB，`chrome-headless-shell` 220 MB linux64 预拉
- hyperframes：`~/.npm/_npx` 31 个 hash 缓存（最新 2026-08-31 10:51）

**AGENT_GUIDE 硬规则触发**：因为三个都可用，animated-explainer 跑到 proposal 时**必须**呈现三个选项让用户选。

## B. Capability 配置（来自 get_provider_menu）

| capability | configured / total | available_providers | 状态 |
|---|---|---|---|
| analysis | 8/14 | ffmpeg, ffprobe, local, multi, transformers | ✅ |
| artifact_delivery | 1/1 | rsync | ✅ |
| asset_management | 4/4 | openmontage | ✅ |
| audio_processing | 2/2 | ffmpeg | ✅ |
| avatar | 0/4 | (kling_official / sadtalker / wav2lip 全空) | ❌ |
| character_animation | 6/6 | openmontage | ✅ |
| clip_acquisition | 1/1 | openmontage | ✅ |
| clip_retrieval | 1/1 | openmontage | ✅ |
| cloud_storage | 13/13 | tencent_weiyun | ✅ |
| corpus_population | 0/1 | (空) | ❌ |
| enhancement | 2/6 | ffmpeg | ⚠️ 偏瘦 |
| external_recompose | 1/1 | claude_video | ✅ |
| graphics | 2/3 | mermaid, pygments | ⚠️ |
| **image_generation** | **3/13** | **minimax, multi, openai** | ⚠️ 偏瘦 |
| image_upload | 1/1 | imgbb | ✅ |
| music_generation | 1/4 | local | ⚠️ 偏瘦 |
| music_library | 0/1 | (空) | ❌ |
| music_search | 1/2 | pixabay_music | ⚠️ |
| publish | 5/5 | local, s3, weiyun | ✅ |
| screen_capture | 2/2 | cap, ffmpeg | ✅ |
| source_ingest | 0/1 | (空) | ❌ |
| subtitle | 2/2 | openmontage, remotion | ✅ |
| translation | 1/2 | nllb | ⚠️ |
| **tts** | **3/10** | **edge_tts, openai, voicebox** | ⚠️ 实查缩水 |
| **video_generation** | **1/24** | **minimax_direct** | ❌ **关键瓶颈** |
| video_post | 9/9 | ffmpeg, hyperframes | ✅ |

**setup_offers 总数**: 46 条（即"再装一个 env var 就解锁某 provider"清单）。完整列表见 `/tmp/pm.json`。

## C. TTS 实查详情

| tool | registry status | 实际可用性 | 备注 |
|---|---|---|---|
| `voicebox_tts` | available | ⚠️ backend 健康但 Python 路径不稳 | 后端 `:17493` `status=healthy, model_loaded=true`，但 `tools.tts.voicebox_tts` import 在某些 PYTHONPATH 下失败；fallback 走 kokoro==0.9.4 KPipeline |
| `edge_tts` | **unavailable** | ❌ 缺 `edge-tts` pip 包 | `install_instructions: pip install edge-tts`；5 秒装好 |
| `piper_tts` | available | ⚠️ 没缓存中文 voice model | `pip install piper-tts` 已装，但需要下载 `.onnx` 模型；`>=1.7` 不再 auto-download |
| `azure_stt` (transcription) | unavailable | ❌ 缺 AZURE_SPEECH_KEY | cloud STT，可选 |
| `kokoro` | (不在 menu) | ✅ 本地 venv 有 | escape hatch per memory |

**结论**：中文 TTS 实查可用路径只有 2 条：
1. `kokoro==0.9.4` KPipeline 直接调（per memory `voicebox-blocked-kokoro-escape-hatch`，HF_HUB_OFFLINE=1）
2. `piper_tts` + 手动下载 zh voice model（首次需下载约 50-100 MB）

## D. 视频生成实查详情

| tool | status | runtime | 视觉质量档 | 估算 cost/段 |
|---|---|---|---|---|
| `minimax_direct` | available | hybrid | 中（API 文档显示 720p/1080p 5-10s） | 待查 |
| kling_official | setup offer | api | 高（kling 2.0 1080p, motion brush） | ~$0.05-0.15/段 |
| runway | setup offer | api | 高（Gen-3/4） | ~$0.30-0.50/段 |
| veo | setup offer | api | 高（Google VEO 3） | ~$0.30-0.60/段 |
| seedance | setup offer | api | 高（字节豆包） | 待查 |
| ltx-modal | setup offer | modal | 中（本地 GPU/Modal） | 待查 |
| heygen / higgsfield / grok / minimax / pixabay / pexels / openai | setup offer | api | 各异 | 各异 |
| **comfyui** | setup offer | local | 看 workflow | ⚠️ **runtime warning**: bundled WAN 2.2 14B FP8 workflow 要 16GB VRAM，8GB 跑不动 |

## E. Setup Offers 优先级（针对 animated-explainer）

按"解锁视觉质量"×"装好成本"排序：

| 优先级 | offer | env var | 装好后解锁什么 |
|---|---|---|---|
| 1 | edge-tts | `pip install edge-tts` | 10+ 高质量中文 TTS |
| 2 | Kling API | `KLING_API_KEY` | 1.6/2.0/2.1 视频生成 |
| 3 | Flux API | `BFL_API_KEY` | FLUX.1/2 图像生成 |
| 4 | Google TTS | `GOOGLE_APPLICATION_CREDENTIALS` | 高质量多语言 TTS |
| 5 | ElevenLabs | `ELEVENLABS_API_KEY` | 顶级 TTS + voice clone |
| 6 | Suno | `SUNO_API_KEY` | 高质量 bgm |
| 7 | Recraft | `RECRAFT_API_KEY` | 矢量 / 风格化图像生成 |

## F. Runtime Warnings

仅 1 条：
> comfyui_video: The top-level resource_profile is a ComfyUI provider floor, not a promise that every workflow fits 8GB VRAM. Bundled WAN 2.2 14B FP8 workflows recommend 16GB VRAM; custom low-VRAM workflows can target 8GB-12GB depending on model, quantization, resolution, and frame count.

含义：当前 GPU 8GB 跑 comfyui 自带的 WAN 2.2 14B 会爆。要么升 GPU，要么自己写低显存 workflow。

## G. Backlot 观测性

- MCP server PID 2069162 在 `:8900` 跑（root, pyenv 3.10.12）
- Tweak sidecar PID 863823 在 `:8901` 跑
- `om_mcp_probe.py` PID 3894146 健康探针在 `:9099`
- `mcp_server.log` 有 `tail -f` 在跑

animated-explainer 跑起来后，Backlot (`python -m backlot open <project-id>`) 会自动派生 board 状态。

## H. 历史快照

- 2026-08-31: 首次拉菜单 + 实查 TTS/视频生成状态
