# bag-video-remix 4 步迭代打磨记录

> 写给后来者：这套 polish pipeline 是怎么从「能用」走到「成片」的。
> 适用于 video-template-remix（或任何 AI 替换 + ffmpeg 拼接的合成流水线）的最后阶段。

## 起点

**`projects/bag-video-remix-2026-08-31/renders/v1_user_assets.mp4`** — V3 bag-hero + 8 个 T2I slot overlay + watermark 移除 + 源音频保留。

**已知 caveat：**
- T2I 是静态帧，1-3 秒场景看起来像 freeze frame
- 没有任何 BGM，干声 + 字幕条
- AI 生成的包袋偏暖偏黄，源片略中性（色温不统一）
- 源硬字幕透出在 AI 图上（字幕说「包包的材质...」但背景是 AI 包，违和）

## 迭代哲学：加性叠加（additive polish）

每一步都是**独立的、可叠加的**——后一步不动前一步已落地的资产。新版本保留旧版本（不覆盖），方便对比和回滚。

| 版本 | 文件 | 增量改动 | 累计改动 |
|---|---|---|---|
| V1 (base) | `v1_user_assets.mp4` | T2I overlay + delogo | — |
| V2 | `v2_kenburns.mp4` | + Ken Burns（slow zoom + 微 pan） | T2I overlay + delogo + KB |
| V3 | `v3_bgm.mp4` | + 轻 BGM（MusicGen local + ducking） | T2I + KB + BGM |
| V4 | `v4_colorgraded.mp4` | + 统一调色（colorbalance + eq） | T2I + KB + BGM + grading |
| V5 | `v5_resubtitled.mp4` | + 字幕重烧（mask 源字幕 + 新 ASS 轨） | T2I + KB + BGM + grading + sub |

**核心思路**：每一步都在前一步基础上**只多一份 ffmpeg filter / 额外 input**，绝不「先全部推翻再来」。这样：
1. 每步出错时只损失一步的 effort
2. 任意中间版本都可作为 deliverable
3. 用户可以选「我只要 V2 视觉感 + V4 调色」拼出自己要的组合

## 每一步的具体做法（后来者复用）

### V2 — Ken Burns on T2I overlays

**问题**：T2I 是 1 张静态图，loop 进 1-3 秒 scene 后是 freeze frame。源视频的 handheld 微晃是 UGC 沉浸感来源之一。

**解法**：ffmpeg `zoompan` filter，在 slot duration 内做 5% slow zoom-in + 中心微 pan。

```
[1:v]scale=1920:1080:force_original_aspect_ratio=increase,
     crop=1920:1080,
     zoompan=z='min(zoom+0.0008,1.05)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':
            d=1:s=1920x1080:fps=30,
     format=yuv420p[ovr]
```

`z` 表达式每帧增加 0.0008，约 30s 增到 1.024 倍。`d=1` 表示每个 input frame 持续 1s（配合 `-loop 1 -framerate 30` 输入）。passthrough scene 不变。

### V3 — BGM (background music)

**问题**：纯人声 + 静音底，听起来像 raw recording。

**解法**：`tools/audio/music_gen_local.py`（MusicGen-small 已 cache 在 `~/.cache/huggingface/hub/models--facebook--musicgen-small/`），prompt 写「lo-fi chill ambient, 90 bpm, soft piano, no vocals」，duration=180s。生成 `assets/audio/bgm.wav`。

混音 ffmpeg filter：
```
[0:a][1:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=1000:makeup=1[ducked];
[ducked]volume=0.18[bgm_ducked];
[0:a]volume=1.0[voice];
[voice][bgm_ducked]amix=inputs=2:duration=longest[aout]
```

效果：人声说话时 BGM 自动降 8:1，说话间隙回升。voice track 保持原音量 1.0，BGM 输出音量 0.18（-15dB），不抢戏。

### V4 — Color grading 统一色温

**问题**：MiniMax T2I 默认偏暖偏黄（典型 MiniMax 暖色调偏好），源片相对中性。

**解法**：ffmpeg `eq` + `colorbalance` filter，统一往中性偏冷推一点。

```
[0:v]eq=brightness=-0.02:saturation=0.92:contrast=1.05:gamma=0.98,
     colorbalance=bs=-0.03:bm=0.02,
     format=yuv420p[vout]
```

应用范围：**只对 overlay segment**（AI 图像），**不动 passthrough scene**（源视频），避免给源画面二次调色。

### V5 — Re-burn subtitles

**问题**：源硬字幕在 AI 图上透出，违和。

**解法**（两步）：
1. **mask 源字幕**：在 overlay segment 加 `delogo=x=0:y=850:w=1920:h=140:show=0`，mask 掉底部 850-990 区域（字幕常规位置）。
2. **烧新字幕轨**：从 `assets/_transcript/*.json` 提取 word-level timestamps，生成 ASS 文件用 ffmpeg `ass` filter 烧轨。字幕样式与源一致（白字深色描边、底部居中、字号匹配）。

ASS 模板：
```
[Script Info]
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei UI,52,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,80,134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:08.91,0:00:16.03,Default,,0,0,0,,大家好我是芝麻...
```

## 成本/时间记录

| 步骤 | 工具 | 成本 | 渲染时间 |
|---|---|---:|---:|
| V1 base | MiniMax T2I × 8 + ffmpeg | $0.024 | ~3 min |
| V2 KB | ffmpeg filter tweak | $0 | +1 min |
| V3 BGM | MusicGen-small local | $0 | +5 min (生成 180s) |
| V4 grading | ffmpeg filter | $0 | +1 min |
| V5 subtitles | faster-whisper ASR + ASS + ffmpeg | $0 | +2 min |
| **合计** | | **$0.024** | **~12 min** |

## 复用要点

1. **每个 ffmpeg filter 都是独立小段** — 出错就单独 re-render 那一段，**不要全量重来**
2. **版本保留而不覆盖** — 旧版本是 reference，新版本出问题立刻能 rollback
3. **PASSTHROUGH vs OVERLAY 段分开处理** — 调色、字幕 mask 只动 overlay 段；BGM / 时长对齐 / 段 concat 影响所有段
4. **Ken Burns 表达式 `0.0008` 是经验值** — 太慢看不出运动，太快变 zoom-in；可以按 scene 时长调整（短 scene 用更大 `0.0008*duration_factor`，长 scene 用更小）
5. **字幕重烧前先验证 ASR 准确度** — 这次 faster-whisper base 错把「D 环」听成「地还」；ASS 用源 hardcoded 字幕原文更稳

## 不在本次范围

- 9:16 竖屏版（重排版 + padding）
- 双语字幕（英文字幕）
- I2V 模式（需要用户提供参考图）
- AI 重生成 agenda-cover 更接近 LV monogram

见 v0/v1 的 `decision_log.json` 全部历史决策。
