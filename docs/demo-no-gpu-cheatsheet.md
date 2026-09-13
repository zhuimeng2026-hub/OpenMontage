# OpenMontage Demo — 无 GPU 速查表

> 给客户演示前的 5 分钟速查。本页所有数字都是 **2026-09-13 在本机 (`/opt/OpenMontage_Voicebox`) 实测**，CPU only，无 GPU。
>
> 数据来源：`/tmp/om_demo_bench/route_a_bench.json`、注册表 `provider_menu_summary()`、`MEMORY.md`。

---

## 一句话结论

**最耗时 = `video_understand`（BLIP-2-OPT-2.7b），本机 CPU fp32 单帧 46s**。Reference-video 流程若不降采样会把演示吞光。

演示**主推路线 A（animated-explainer）**：6 段 30s = **约 5 分 39 秒**，成本 **$0.018**。

---

## 路线 A 端到端实测（本机，无 GPU）

| 阶段 | 调用 | 实测耗时 | 备注 |
|---|---|---|---|
| 图像生成 × 6 | `minimax_image` (image-01, 16:9) | **149.0s**（均值 24.8s, min 15.3s, max **59.5s**——API 偶发排队）| 串行；如并发 image+image 可压缩到 ~80s |
| TTS × 6 | `edge_tts` (en-US-AriaNeural) | **9.0s**（均值 1.5s）| 完全可忽略 |
| 音频拼接 | `ffmpeg -c copy` concat | **0.11s** | |
| Remotion 渲染 | `npx remotion render Explainer` 30s × 30fps | **181s** | 比官方 baseline（china-tech 27s = 134s）慢 35%——多了 6 张图合成 |
| 音频混流 | `ffmpeg -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -af "aresample=44100" -movflags +faststart` | **0.27–0.73s** | 显式 `-map` 是必须的；省略会让 ffmpeg 从无音轨视频"凑"一条 AAC，写出 2.3 kbps 空流（实测 -91 dB 静默）。见下方"FFmpeg 静音坑"章节 |
| **端到端总计** | | **339.4s ≈ 5m39s** | 产出 31s / 4.99 MB mp4 |

**成本**：6 × $0.003 = **$0.018**（仅图像）；TTS / 渲染 / 拼接零成本。

**对照 baseline**：注册表里现成的 `china-tech-2026.json`（27s，含柱状图动画）= 134s。说明**纯文本 + 图表 ≈ 5× realtime**；**文本 + 6 张背景图 ≈ 6× realtime**。

> 实测产物：`/tmp/om_demo_bench/render/route-a-final.mp4`（31s, 5.0 MB）

---

## 能力时间成本分级（演示避坑用）

### 🟢 无 GPU 下"安全 + 快"（演示主力）

| 能力 | 推荐 tool | 典型耗时 |
|---|---|---|
| 云端图像 | `minimax_image` / `openai_image` | 5–20s/张 |
| 云端 TTS | `edge_tts`（免费）/ `openai_tts` | 1–3s/段 |
| 云端视频 | `minimax_video_direct`（**配额宝贵，3 次/计划**）| 60–120s/6s 段 |
| 配乐来源 | `pixabay_music`（搜索 stock）| 秒级 |
| 转录（CPU OK）| `transcriber`（faster-whisper，本地已装）| 10s 音 ≈ 3–5s |
| 场景切分（仅切分）| `transnetv2` + ffmpeg 抽关键帧 | 10–30s/视频 |
| 构图渲染 | `video_compose` → Remotion / HyperFrames / FFmpeg | 30–180s/30s 视频 |
| 字幕烧录 | `subtitle_gen`（Remotion word-level）| < 30s |
| 拼接 / 裁剪 / 混音 | `video_stitch` / `video_trimmer` / `audio_mixer` | 秒级 |
| 屏幕录制 | `cap_recorder` / `screen_capture_selector` | 取决于场景 |
| 终端动画 | Remotion `TerminalScene` 合成（不录屏）| 秒级 |

### 🟡 中等耗时（演示可用，但要预告等待）

| 能力 | tool | 耗时 | 备注 |
|---|---|---|---|
| 参考视频综合分析 | `video_analyzer` | 10–30 分钟/60s 视频 | 内部调 `video_understand`；演示前降采样到 ≤10s + 6 帧 |
| 本地音乐生成 | `music_gen_local`（MusicGen-small 300MB）| 2.5–5 分钟/30s 配乐 | CPU 推理 5–10× realtime；建议走 `pixabay_music` |

### 🔴 不能演示（要 GPU 或配额/网络问题）

| 能力 | tool | 原因 |
|---|---|---|
| 视频帧语义理解（按帧）| `video_understand` | 本机 CPU 46s/帧（BLIP-2-OPT-2.7b，记忆固化值）|
| 本地图像生成 | `local_diffusion` / `comfyui_image` | 工具自己标注 "CPU-only machines (very slow)" |
| 本地视频生成 | `wan_video` / `hunyuan_video` / `ltx_video_local` / `cogvideo_video` | 工具自己标注 "CPU-only machines" |
| 视频超分 / 人脸修复 | `upscale` / `face_restore` / `bg_remove` / `rembg` | 全要 GPU 或第三方 key |
| Avatar / Lip-sync | `kling_avatar` / `kling_lip_sync` / `talking_head`（SadTalker）/ `lip_sync`（Wav2Lip）| 后两个要 CUDA；前者要 `KLING_API_KEY` |
| Manim 图表 | `manim` | 注册表 unavailable |
| 本地 TTS | `piper_tts` / `voicebox_tts` | 注册表 unavailable；voicebox 还有 HF proxy 阻塞（记忆）|

---

## 三条推荐演示路径

### 路线 A：animated-explainer（**主推**）

```
idea → script → scene_plan → assets（minimax_image + edge_tts）→ edit → compose（Remotion）
```

- **演示时间**：**~5m39s** 实测
- **演示亮点**：playbook 切换、Remotion 弹簧动画、word-level 字幕、麦克风级配音
- **风险**：零；纯云端生成 + 本地 Node 渲染
- **预热**：提前生成好 6 张图 + 6 段 TTS，现场只跑渲染和混流（30s 内出片）

### 路线 B：screen-demo（终端动画 + 旁白，最稳）

```
Remotion TerminalScene（合成"假终端"）+ edge_tts 旁白
```

- **演示时间**：1–2 分钟
- **演示亮点**：不依赖任何 GPU、零外部 API key、可放 4K
- **风险**：零；全本地

### 路线 C：video-template-remix（敢秀但要预热）

```
参考视频 → video_analyzer（限制 ≤6 帧 / 0.5fps）→ 自动 remix
```

- **演示时间**：5–10 分钟（demo 当场跑客户会等）
- **演示亮点**：reference-driven 工作流，差异化卖点
- **风险**：必须**先 dry-run 一次**；避开 minimax 配额（记忆 `minimax-hailuo-2-3-quota-2067.md`）

---

## 🚨 演示现场红线

| 客户说 | 你答 | 原因 |
|---|---|---|
| "AI 自动分析参考视频并出 brief" | 立即问视频多长；>30s 必走降采样（≤6 帧 / 0.5fps）| 否则 BLIP-2 单帧 46s 吞光 |
| "AI 生成一段开场镜头" | 限 1 段 6s；演示后立即记录已用次数 | minimax 配额 3 次后 2067 |
| "本地跑 Stable Diffusion / Wan" | 不支持，需要 GPU | `local_diffusion` unavailable |
| "用 SadTalker 出数字人" | 不支持，需要 GPU + 模型权重 | `talking_head` unavailable |
| "用 voicebox TTS 配音" | 改用 edge_tts | 本机 voicebox 运行时挂（HF proxy）|

---

## 演示前 5 分钟 checklist

```bash
# 1. 确认 MCP / Remotion / edge_tts / minimax 通路
make preflight | head -50

# 2. 确认 node + remotion-composer/node_modules
cd remotion-composer && ls node_modules/.bin/remotion && cd ..

# 3. 确认 .env 里有 MINIMAX_API_KEY 和 OPENAI_API_KEY
grep -E "MINIMAX|OPENAI" .env | sed 's/=.*/=***/'

# 4. 探测一条 minimax_image（避免现场配额 2067 才发现）
python -c "from tools.tool_registry import registry; r=registry; r.discover(); \
print(r._tools['minimax_image'].execute({'prompt':'warmup','aspect_ratio':'16:9','output_path':'/tmp/warmup.png'}).success)"

# 5. 准备 demo 资产（路线 A 预热，6 张图 + 6 段 TTS 已就位 → /tmp/om_demo_bench/）

# 6. 演示现场只跑渲染 + 混流（< 1 分钟出片）：
bash -c 'cd remotion-composer && \
  npx --no-install remotion render src/index.tsx Explainer \
    /tmp/om_demo_bench/render/route-a-silent.mp4 \
    --props /tmp/om_demo_bench/route-a-props.json --codec h264'
ffmpeg -y -i /tmp/om_demo_bench/render/route-a-silent.mp4 \
       -i /tmp/om_demo_bench/audio/narration.mp3 \
       -c:v copy -c:a aac -shortest /tmp/om_demo_bench/render/route-a-final.mp4
```

---

## 已知陷阱（演示前 24h 已固化到记忆）

| 记忆条目 | 影响 |
|---|---|
| `minimax-hailuo-2-3-quota-2067.md` | T2V/I2V 用完 3 次后 2067；演示前先 probe |
| `minimax-hailuo-safety-softening-violence.md` | 暴力/破坏类提示词被软化；演示别用 |
| `voicebox-blocked-kokoro-escape-hatch.md` | voicebox_tts 运行时挂；用 edge_tts 替代 |
| `voicebox-huggingface-proxy-requirement.md` | voicebox 服务需 HTTPS_PROXY |
| `blip2-opt-2.7b-sharded-safetensors-cache-layout.md` | BLIP-2 safetensors 必须用 glob 加载；本机 CPU 单帧 46s |
| `whisper-availability-frozen-verdict.md` | faster-whisper 可用；不要说 "unavailable" |
| `tts-registry-vs-runtime-gap.md` | 注册表 `available` ≠ 运行时可用；smoke-test 再用 |

---

## 配乐 genre 速查（pixabay_music 实测，2026-09-13）

**结论先说**：OM 的 `pixabay_music` 工具**只暴露 `query` 自由文本**，没有 genre 枚举字段；Pixabay Music 库实际有 **~50,000+ 首**免费曲目。"20 hits" 是 **Pixabay 翻页上限**，不是该 genre 总数。

**22 个 genre / mood 实测有效**（返回 ≥20 hits）：

| 类别 | 关键词 |
|---|---|
| **场景 / 风格** | `cinematic` · `epic` · `corporate` · `electronic` · `lofi` · `classical` |
| **流派** | `ambient` · `hip-hop` · `jazz` · `pop` · `rock` |
| **情绪** | `happy` · `sad` · `energetic` · `calm` · `romantic` · `dark` · `mysterious` · `inspiring` · `motivational` · `uplifting` |
| **用途** | `background` · `tutorial` · `advertising` |

**3 个 query 实测失败**（OM 工具报 "Pixabay returned no music results"）：

- `folk` — 改用 `acoustic folk` / `country folk` 复合 query 绕过
- `dramatic` — 改用 `epic dramatic` / `cinematic dramatic`
- `presentation` — 改用 `corporate presentation` / `business presentation`

**已知盲区**（这套词没测但可能也空）：Christmas / Halloween / Wedding / Sports / Gaming / Travel / Cooking / Podcast / Vlog / Meditation

### 调 `pixabay_music` 的最佳实践

```python
# 复合关键词 + 时长约束 → 命中率高
registry._tools['pixabay_music'].execute({
    'query': 'cinematic emotional piano',   # 3 词组合，genre + 情绪 + 乐器
    'min_duration': 25,                    # 至少覆盖视频长度
    'max_duration': 180,                   # 太长的 ffmpeg -t 截掉
})
```

### 配乐通路优先级（无 GPU，无 ElevenLabs key）

| 通路 | 状态 | 何时用 |
|---|---|---|
| `pixabay_music` | ✅ available | **默认首选**；免费、秒级、stock 库大 |
| `music_gen_local`（MusicGen-small）| ✅ available | **自定义情绪但找不到合适的 stock** 时；CPU 2.5–5 分钟/30s |
| `music_gen`（ElevenLabs Music）| ❌ unavailable | 需要 `ELEVENLABS_API_KEY` |
| `suno_music` | ❌ unavailable | 渠道未配 |
| `freesound` | ❌ unavailable | `music_search` 能力下未配 |
| `music_library/`（本地预载）| ❌ 空目录 | 客户可手动 `cp` 自己的曲目进 `music_library/` |

**演示前**：如果客户风格有偏好（如"必须用钢琴曲"），提前用 2–3 个 query 探一下，**别在客户面前第一次跑**。

---

## FFmpeg 静音坑（2026-09-13 实测踩到）

**症状**：mux 出来的 mp4 播放器无声；`volumedetect` 报告 `mean_volume: -91 dB`、`max_volume: -91 dB`；`ffprobe` 报的 audio bitrate 在 **2276 bps** 量级（正常人声应 64–192 kbps）。

**根因**：默认 `ffmpeg -i video.mp4 -i audio.mp3 -c:v copy -c:a aac -shortest` 让 ffmpeg 自动选流，它会从无音轨的视频输入"凑"出一条 AAC 流，输出的不是音频是空壳。

**修复**：强制显式 stream mapping + 显式 AAC 比特率 + 采样率重采样：

```bash
ffmpeg -y -i silent.mp4 -i narration.mp3 \
  -map 0:v -map 1:a \
  -c:v copy -c:a aac -b:a 192k \
  -af "aresample=44100" \
  -movflags +faststart \
  out.mp4
```

**验收检查**（每次 mux 完必跑）：

```bash
ffmpeg -hide_banner -i out.mp4 -af "volumedetect" -vn -f null - 2>&1 | grep volume
# mean_volume 应在 -25 ~ -15 dB；max_volume 应在 -10 ~ -3 dB
# 若两个都在 -90 dB 附近 → 重 mux 加 -map

ffprobe -v error -show_streams -of json out.mp4 | python -c "
import json,sys; s=json.load(sys.stdin)['streams']
for x in s:
    if x['codec_type']=='audio':
        print(f'audio: {x[\"codec_name\"]} {x[\"sample_rate\"]}Hz {x[\"channels\"]}ch bitrate={int(x[\"bit_rate\"])/1000:.0f}kbps')
"
# audio bitrate 应 ≥ 64 kbps（128 / 192 / 256 都行）；< 10 kbps = 静音
```

---

## 复现基准的方法

```bash
# 完整 benchmark（约 6 分钟，含 Remotion 渲染）
mkdir -p /tmp/om_demo_bench/{images,audio,render}
# 1) 跑 6 张图 + 6 段 TTS（见上文 asset_timings.json 生成脚本）
# 2) 渲染（baseline）： china-tech 27s 134s
# 3) 渲染（路线 A）： 30s 181s
# 4) 音频拼接 + 混流：< 1s

# 单点对比：仅 BLIP-2 单帧 46s
python -c "
from transformers import Blip2Processor, Blip2ForConditionalGeneration
import torch, time
proc = Blip2Processor.from_pretrained('Salesforce/blip2-opt-2.7b')
m = Blip2ForConditionalGeneration.from_pretrained('Salesforce/blip2-opt-2.7b', torch_dtype=torch.float32)
# ... 单帧推理 ~46s on CPU
"
```

---

## 关键联系人

| 问题 | 找谁 / 查哪 |
|---|---|
| 注册表 / 工具状态 | `python -c "from tools.tool_registry import registry; registry.discover(); print(registry.provider_menu_summary())"` |
| 当前 pipeline 阶段 | `python -m backlot open <project-id>`（live storyboard）|
| Stage director 怎么走 | `skills/pipelines/<pipeline>/<stage>-director.md` |
| 工具的 Layer 3 skill | 看 `tool.get_info()["agent_skills"]` → `.agents/skills/<name>/SKILL.md` |
| 决策日志 | `projects/<id>/decision_log.json`（append-only）|

---

_文档维护者_：OpenMontage agent • _最近更新_：2026-09-13（Route A 实测）
